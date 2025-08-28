import os
import logging
import telebot.asyncio_helper as asyncio_helper
from telebot.async_telebot import AsyncTeleBot
import ccxt.pro as ccxt
from decimal import Decimal, getcontext, InvalidOperation
import traceback
import asyncio
from datetime import datetime
import json

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
BLACKLIST_MOEDAS = {'TON', 'SUI', 'PI'}
ORDER_BOOK_DEPTH = 100
API_TIMEOUT_SECONDS = 60
VERBOSE_ERROR_LOGGING = True
MAX_RECONNECT_ATTEMPTS = 5

# --- Log Handlers ---
class TelegramHandler(logging.Handler):
    """
    Handler de log para enviar mensagens de CRITICAL para o Telegram.
    A mensagem de CRITICAL agora é usada apenas para erros inesperados.
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
                self.bot.send_message(self.chat_id, f"🔴 **ERRO CRÍTICO NO BOT!**\n\n`{log_entry}`", parse_mode="Markdown"),
                self.loop
            )
        except Exception as e:
            print(f"Falha ao enviar log para o Telegram: {e}")

# --- Helper Function for Decimal Conversion ---
def safe_decimal(value, default_value=Decimal('0')):
    """
    Converte um valor para Decimal de forma segura.
    Retorna default_value se o valor for None, vazio ou não puder ser convertido.
    """
    if value is None or value == "":
        return default_value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        logging.warning(f"Erro de conversão: valor '{value}' inválido para Decimal. Retornando padrão.")
        return default_value

def load_persistent_blacklist():
    """Carrega a blacklist persistente de pares de um arquivo."""
    try:
        with open('persistent_blacklist.json', 'r') as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_persistent_blacklist(blacklist):
    """Salva a blacklist persistente de pares em um arquivo."""
    try:
        with open('persistent_blacklist.json', 'w') as f:
            json.dump(list(blacklist), f)
    except IOError as e:
        logging.error(f"Falha ao salvar a blacklist persistente: {e}")

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
PERSISTENT_BLACKLIST = load_persistent_blacklist()

# --- Command Handlers ---
async def send_welcome(message):
    await bot.reply_to(message, "Bot v39.1 (Bot de Arbitragem) está online. Use /status.")

async def send_balance_command(message):
    try:
        await bot.reply_to(message, "Buscando saldos na OKX...")
        balance = await exchange.fetch_balance()
        reply = "📊 **Saldos (OKX):**\n"
        for moeda in MOEDAS_BASE_OPERACIONAIS:
            saldo = balance.get(moeda, {'free': 0, 'total': 0})
            saldo_livre = safe_decimal(saldo.get('free', '0'))
            saldo_total = safe_decimal(saldo.get('total', '0'))
            reply += (f"- `{moeda}`\n"
                      f"  Disponível para negociação: `{saldo_livre:.4f}`\n"
                      f"  Total (incluindo ordens): `{saldo_total:.4f}`\n")
        
        await bot.send_message(message.chat.id, reply, parse_mode="Markdown")
    except Exception as e:
        await bot.reply_to(message, f"❌ Erro ao buscar saldos: {e}")
        logging.error(f"Erro no comando /saldo: {e}")

async def send_status(message):
    status_text = "Rodando" if state['is_running'] else "Pausado"
    mode_text = "Simulação" if state['dry_run'] else "⚠️ MODO REAL ⚠️"
    
    persistent_blacklist_count = len(PERSISTENT_BLACKLIST)
    persistent_blacklist_text = f"Pares em blacklist permanente: `{persistent_blacklist_count}`" if persistent_blacklist_count > 0 else "Sem pares em blacklist permanente."

    reply = (f"Status: {status_text}\n"
             f"Modo: **{mode_text}**\n"
             f"Lucro Mínimo: `{state['min_profit']:.4f}%`\n"
             f"Volume de Negociação: `{state['volume_percent']:.2f}%`\n"
             f"Profundidade Máxima da Rota: `{state['max_depth']}`\n"
             f"{persistent_blacklist_text}")
    await bot.send_message(message.chat.id, reply, parse_mode="Markdown")

async def simple_commands(message):
    command = message.text.split('@')[0][1:]
    if command == 'pausar':
        state['is_running'] = False
        await bot.reply_to(message, "Engine pausado.")
    elif command == 'retomar':
        state['is_running'] = True
        await bot.reply_to(message, "Engine retomado.")
    elif command == 'modo_real':
        state['dry_run'] = False
        await bot.reply_to(message, "⚠️ MODO REAL ATIVADO! ⚠️ As próximas oportunidades serão executadas.")
    elif command == 'modo_simulacao':
        state['dry_run'] = True
        await bot.reply_to(message, "Modo de simulação ativado.")
    logging.info(f"Comando '{command}' executado.")

async def value_commands(message):
    try:
        parts = message.text.split(maxsplit=1)
        command = parts[0].split('@')[0][1:]
        value = parts[1] if len(parts) > 1 else ""

        if command == 'setlucro':
            state['min_profit'] = safe_decimal(value, state['min_profit'])
            await bot.reply_to(message, f"Lucro mínimo definido para {state['min_profit']:.4f}%")
        elif command == 'setvolume':
            vol = safe_decimal(value)
            if 0 < vol <= 100:
                state['volume_percent'] = vol
                await bot.reply_to(message, f"Volume de negociação definido para {state['volume_percent']:.2f}%")
            else:
                await bot.reply_to(message, "O volume deve estar entre 1 e 100.")
        elif command == 'setdepth':
            depth = safe_decimal(value, 0)
            if MIN_ROUTE_DEPTH <= depth <= 5:
                state['max_depth'] = int(depth)
                await bot.reply_to(message, f"Profundidade da rota definida para {state['max_depth']}. O mapa será reconstruído no próximo ciclo.")
            else:
                await bot.reply_to(message, f"A profundidade deve estar entre {MIN_ROUTE_DEPTH} e 5.")
        
        logging.info(f"Comando '{command} {value}' executado.")
    except Exception as e:
        await bot.reply_to(message, f"Erro no comando. Uso: /{command} <valor>")
        logging.error(f"Erro ao processar comando '{message.text}': {e}")
        
async def check_websocket_status(message):
    try:
        start_time = datetime.now()
        timeout = 60
        
        while not engine.order_books and (datetime.now() - start_time).total_seconds() < timeout:
            await asyncio.sleep(1)
        
        if not engine.order_books:
            await bot.reply_to(message, "❌ **O engine ainda não começou a monitorar os livros de oferta.** Verifique se o bot está rodando e se há rotas válidas.")
            return

        report = "🔍 **Status da Conexão WebSocket**\n"
        current_time = datetime.now()
        
        active_count = 0
        stale_count = 0
        details = []

        for symbol, orderbook in engine.order_books.items():
            if 'timestamp' in orderbook:
                last_update_ms = orderbook['timestamp']
                last_update_s = last_update_ms / 1000.0
                last_update_dt = datetime.fromtimestamp(last_update_s)
                time_diff_s = (current_time - last_update_dt).total_seconds()
                
                status_emoji = "✅" if time_diff_s < 10 else "⚠️"
                
                details.append(f"{status_emoji} `{symbol}` - Última atualização: `{time_diff_s:.2f}s` atrás.")
                
                if time_diff_s < 10:
                    active_count += 1
                else:
                    stale_count += 1
            else:
                details.append(f"❓ `{symbol}` - Sem dados de timestamp.")
                
        report += f"**Resumo:** `{active_count}` pares estão ativos. `{stale_count}` pares estão desatualizados.\n\n"
        
        full_message = report + "\n".join(details)
        
        # Split and send the message in chunks
        max_length = 4096
        for i in range(0, len(full_message), max_length):
            chunk = full_message[i:i+max_length]
            await bot.send_message(message.chat.id, chunk, parse_mode="Markdown")

    except Exception as e:
        await bot.reply_to(message, f"❌ Erro ao verificar o status do WebSocket: {e}")
        logging.error(f"Erro no comando /verificar_ws: {e}")

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
        self.websocket_tasks = {}
        self.persistent_blacklist = PERSISTENT_BLACKLIST
        
    def construir_rotas(self):
        logging.info("Construindo mapa de rotas...")
        self.graph = {}
        self.rotas_viaveis = []

        active_markets = {
            s: m for s, m in self.markets.items()
            if m.get('active')
            and m.get('base') and m.get('quote')
            and m['base'] not in FIAT_CURRENCIES and m['quote'] not in FIAT_CURRENCIES
            and m['base'] not in BLACKLIST_MOEDAS and m['quote'] not in BLACKLIST_MOEDAS
            and s not in self.persistent_blacklist
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
        logging.info(f"Mapa de rotas reconstruído para profundidade {self.last_depth}. {len(self.rotas_viaveis)} rotas encontradas.")
        asyncio.create_task(bot.send_message(CHAT_ID, f"🗺️ Mapa de rotas reconstruído para profundidade {self.last_depth}. {len(self.rotas_viaveis)} rotas encontradas."))

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
                        preco, quantidade_disponivel = safe_decimal(preco_str), safe_decimal(quantidade_str)
                        if preco == 0: continue
                        custo_nivel = preco * quantidade_disponivel
                        if valor_a_gastar >= custo_nivel:
                            quantidade_comprada += quantidade_disponivel
                            valor_a_gastar -= custo_nivel
                        else:
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
                        preco, quantidade_disponivel = safe_decimal(preco_str), safe_decimal(quantidade_str)
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
            raise Exception(f"Erro na simulação para a rota {' -> '.join(cycle_path)}: {e}")

    async def _executar_trade_async(self, cycle_path, volume_a_usar):
        base_moeda = cycle_path[0]
        asyncio.create_task(bot.send_message(CHAT_ID, f"🚀 **MODO REAL** 🚀\nIniciando execução da rota: `{' -> '.join(cycle_path)}`\nInvestimento planejado: `{volume_a_usar:.8f} {base_moeda}`", parse_mode="Markdown"))

        moedas_presas = []
        current_asset = base_moeda
        
        live_balance = await self.exchange.fetch_balance()
        initial_investment_value_real = safe_decimal(live_balance.get(base_moeda, {}).get('free', '0'))
        
        current_amount = initial_investment_value_real * MARGEM_DE_SEGURANCA

        if current_amount < MINIMO_ABSOLUTO_DO_VOLUME:
            await bot.send_message(CHAT_ID, f"❌ **FALHA NA ROTA!** Saldo de `{current_amount:.2f} {current_asset}` está abaixo do mínimo para negociação (`{MINIMO_ABSOLUTO_DO_VOLUME:.2f} {current_asset}`).", parse_mode="Markdown")
            return

        try:
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                if not pair_id: raise Exception(f"Par inválido {coin_from}/{coin_to}")

                try:
                    market_info = self.exchange.markets[pair_id]
                    min_amount = safe_decimal(market_info['limits']['amount']['min']) if 'min' in market_info['limits']['amount'] else Decimal('0')
                    min_cost = safe_decimal(market_info['limits']['cost']['min']) if 'min' in market_info['limits']['cost'] else Decimal('0')
                except (KeyError, TypeError) as e:
                    raise Exception(f"Erro ao obter limites do par {pair_id}: {e}")

                trade_volume_precisao = None
                if side == 'buy':
                    ticker = await self.exchange.fetch_ticker(pair_id)
                    price_to_use = safe_decimal(ticker['ask'])
                    if price_to_use == 0: raise Exception(f"Preço 'ask' inválido (zero) para o par {pair_id}.")
                    amount_to_buy = current_amount / price_to_use
                    
                    trade_volume_precisao = self.exchange.amount_to_precision(pair_id, float(amount_to_buy))
                    trade_volume_precisao_decimal = safe_decimal(trade_volume_precisao)

                    if trade_volume_precisao_decimal == Decimal('0'):
                        raise Exception(f"Volume calculado ({amount_to_buy}) ajustado para a precisão do par ({trade_volume_precisao}) resultou em zero. Ordem inválida.")
                    
                    trade_cost = trade_volume_precisao_decimal * price_to_use

                    if trade_volume_precisao_decimal < min_amount:
                        raise Exception(f"Volume calculado e formatado ({trade_volume_precisao_decimal:.8f}) é menor que o volume mínimo do par ({min_amount:.8f}) para {pair_id}.")
                    
                    if trade_cost < min_cost:
                        raise Exception(f"Valor calculado ({trade_cost:.8f}) é menor que o custo mínimo do par ({min_cost:.8f}) para {pair_id}.")
                    
                    diag_msg = (f"🔍 **DIAGNÓSTICO DA ORDEM**\n"
                                f"Par: `{pair_id.replace('/', '_')}`\n"
                                f"Lado: `COMPRA`\n"
                                f"Volume: `{trade_volume_precisao}`\n"
                                f"Preço de Execução Estimado: `{price_to_use:.8f}`")
                    await bot.send_message(CHAT_ID, diag_msg, parse_mode="Markdown")
                    
                    logging.info(f"✅ DIAGNÓSTICO: Tentando COMPRAR {trade_volume_precisao} {coin_to} com {current_amount} {coin_from} no par {pair_id}")
                    order = await self.exchange.create_market_buy_order(pair_id, trade_volume_precisao)

                else:
                    trade_volume_precisao = self.exchange.amount_to_precision(pair_id, float(current_amount))
                    trade_volume_precisao_decimal = safe_decimal(trade_volume_precisao)

                    if trade_volume_precisao_decimal == Decimal('0'):
                        raise Exception(f"Volume calculado ({current_amount}) ajustado para a precisão do par ({trade_volume_precisao}) resultou em zero. Ordem inválida.")

                    if trade_volume_precisao_decimal < min_amount:
                        raise Exception(f"Volume calculado e formatado ({trade_volume_precisao_decimal:.8f}) é menor que o volume mínimo do par ({min_amount:.8f}) para {pair_id}.")

                    estimated_price = safe_decimal(self.exchange.order_books[pair_id]['bids'][0][0])
                    trade_cost = trade_volume_precisao_decimal * estimated_price
                    
                    if trade_cost < min_cost:
                         raise Exception(f"Valor calculado ({trade_cost:.8f}) é menor que o custo mínimo do par ({min_cost:.8f}) para {pair_id}.")

                    diag_msg = (f"🔍 **DIAGNÓSTICO DA ORDEM**\n"
                                f"Par: `{pair_id.replace('/', '_')}`\n"
                                f"Lado: `VENDA`\n"
                                f"Volume: `{trade_volume_precisao}`")
                    await bot.send_message(CHAT_ID, diag_msg, parse_mode="Markdown")
                    
                    logging.info(f"✅ DIAGNÓSTICO: Tentando VENDER com {trade_volume_precisao} {coin_from} no par {pair_id}")
                    order = await self.exchange.create_market_sell_order(pair_id, trade_volume_precisao)

                order_status = await self.exchange.fetch_order(order['id'], pair_id)
                if order_status['status'] != 'closed': raise Exception(f"A ordem {order['id']} não foi totalmente preenchida. Status: {order_status['status']}")

                live_balance = await self.exchange.fetch_balance()
                current_amount = safe_decimal(live_balance.get(coin_to, {}).get('free', '0'))
                current_asset = coin_to
                moedas_presas.append({'symbol': current_asset, 'amount': current_amount})

        except Exception as leg_error:
            error_msg = str(leg_error)
            logging.critical(f"FALHA NA ETAPA {i+1} ({coin_from}->{coin_to}): {error_msg}")
            
            # Adiciona o par à blacklist permanente e salva o arquivo
            PERSISTENT_BLACKLIST.add(pair_id)
            save_persistent_blacklist(PERSISTENT_BLACKLIST)
            
            mensagem_detalhada = f"Erro na etapa {i+1} da rota: `{error_msg}`\n\n**O par foi adicionado à blacklist permanente.**"
            await bot.send_message(CHAT_ID, f"🔴 **FALHA NA ROTA!**\n{mensagem_detalhada}", parse_mode="Markdown")

            if moedas_presas:
                ativo_preso_details = moedas_presas[-1]
                ativo_symbol = ativo_preso_details['symbol']
                await bot.send_message(CHAT_ID, f"⚠️ **CAPITAL PRESO!**\nAtivo: `{ativo_symbol}`.\n**Iniciando venda de emergência de volta para {base_moeda}...**", parse_mode="Markdown")

                try:
                    live_balance = await self.exchange.fetch_balance()
                    ativo_amount = safe_decimal(live_balance.get(ativo_symbol, {}).get('free', '0'))
                    if ativo_amount == 0: raise Exception("Saldo real do ativo preso é zero. Não é possível resgatar.")

                    reversal_pair, reversal_side = self._get_pair_details(ativo_symbol, base_moeda)
                    if not reversal_pair: raise Exception(f"Par de reversão {ativo_symbol}/{base_moeda} não encontrado.")

                    if reversal_side == 'buy':
                        reversal_amount = self.exchange.amount_to_precision(reversal_pair, float(ativo_amount))
                        await self.exchange.create_market_buy_order(reversal_pair, reversal_amount)
                    else:
                        reversal_amount = self.exchange.amount_to_precision(reversal_pair, float(ativo_amount))
                        await self.exchange.create_market_sell_order(reversal_pair, reversal_amount)

                    await bot.send_message(CHAT_ID, f"✅ **Venda de Emergência EXECUTADA!** Resgatado: `{safe_decimal(reversal_amount):.8f} {ativo_symbol}`", parse_mode="Markdown")
                except Exception as reversal_error:
                    await bot.send_message(CHAT_ID, f"❌ **FALHA CRÍTICA NA VENDA DE EMERGÊNCIA:** `{reversal_error}`. **VERIFIQUE A CONTA MANUALMENTE!**", parse_mode="Markdown")
            return

        live_balance_final = await self.exchange.fetch_balance()
        final_amount = safe_decimal(live_balance_final.get(base_moeda, {}).get('free', '0'))
        lucro_real_usdt = final_amount - initial_investment_value_real
        if initial_investment_value_real == Decimal('0'): lucro_real_percent = Decimal('0')
        else: lucro_real_percent = (lucro_real_usdt / initial_investment_value_real) * 100

        await bot.send_message(CHAT_ID, f"✅ **SUCESSO! Rota Concluída.**\nRota: `{' -> '.join(cycle_path)}`\nLucro: `{lucro_real_usdt:.4f} {base_moeda}` (`{lucro_real_percent:.4f}%`)", parse_mode="Markdown")
    
    async def _manage_websocket_task(self, symbol):
        """
        Gerencia uma conexão WebSocket para um único par de moedas.
        """
        reconnect_attempts = 0
        while reconnect_attempts < MAX_RECONNECT_ATTEMPTS:
            try:
                if symbol in self.persistent_blacklist:
                    logging.info(f"O par {symbol} está na blacklist permanente. Não será monitorado.")
                    return

                logging.info(f"Iniciando a escuta do livro de ofertas para {symbol} via WebSocket...")
                
                ws_book = await self.exchange.watch_order_book(symbol, limit=ORDER_BOOK_DEPTH)
                self.order_books[symbol] = ws_book
                logging.info(f"Inscrição no livro de ofertas de {symbol} feita com sucesso.")

                while True:
                    await asyncio.sleep(1)

            except asyncio.CancelledError:
                logging.info(f"Tarefa de WebSocket para {symbol} foi cancelada.")
                await self._unsubscribe_from_order_book(symbol)
                break

            except ccxt.NetworkError as e:
                logging.warning(f"Erro de rede para {symbol}. Tentativa de reconexão {reconnect_attempts + 1}/{MAX_RECONNECT_ATTEMPTS}...")
                if VERBOSE_ERROR_LOGGING:
                    logging.debug(f"Detalhes do erro: {e}")
                reconnect_attempts += 1
                await asyncio.sleep(10)

            except Exception as e:
                logging.error(f"Erro inesperado no WebSocket para {symbol}: {e}. Adicionando par à lista de problemáticos.")
                if VERBOSE_ERROR_LOGGING:
                    logging.debug(traceback.format_exc())
                # Adiciona o par à blacklist permanente e sai
                PERSISTENT_BLACKLIST.add(symbol)
                save_persistent_blacklist(PERSISTENT_BLACKLIST)
                await bot.send_message(CHAT_ID, f"❌ **ERRO NO WEBSOCKET!**\nO par `{symbol}` apresentou um erro e foi adicionado à blacklist permanente para evitar problemas futuros.", parse_mode="Markdown")
                await self._unsubscribe_from_order_book(symbol)
                return


    async def _unsubscribe_from_order_book(self, symbol):
        """Cancela a assinatura de um livro de ofertas."""
        try:
            if symbol in self.exchange.subscriptions:
                await self.exchange.close()
                logging.info(f"Assinatura de {symbol} cancelada e conexão fechada.")
            if symbol in self.order_books:
                del self.order_books[symbol]
        except Exception as e:
            logging.error(f"Erro ao cancelar a assinatura de {symbol}: {e}")

    async def run_arbitrage_loop_inner(self):
        """O loop de arbitragem que pode falhar e ser reiniciado."""
        logging.info("Iniciando loop principal de arbitragem...")
        self.construir_rotas()
        
        while True:
            if not state['is_running']:
                logging.info("Bot pausado. Aguardando comando para retomar...")
                while not state['is_running']:
                    await asyncio.sleep(1)
                logging.info("Bot retomado. Continuanddo operação.")
            
            volumes_a_usar = {}
            balance = await self.exchange.fetch_balance()
            for moeda in MOEDAS_BASE_OPERACIONAIS:
                saldo_disponivel = safe_decimal(balance.get(moeda, {}).get('free', '0'))
                volumes_a_usar[moeda] = (saldo_disponivel * (state['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA
            
            if self.last_depth != state['max_depth']:
                self.construir_rotas()

            required_pairs = set()
            for rota in self.rotas_viaveis:
                for i in range(len(rota) - 1):
                    pair_id, _ = self._get_pair_details(rota[i], rota[i+1])
                    if pair_id and pair_id not in self.persistent_blacklist:
                        required_pairs.add(pair_id)
            
            for pair in required_pairs:
                if pair not in self.websocket_tasks or self.websocket_tasks[pair].done():
                    if pair in self.websocket_tasks:
                        logging.warning(f"Tarefa de WS para {pair} finalizou inesperadamente. Reiniciando...")
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
                        msg = f"✅ **OPORTUNIDADE**\nLucro: `{resultado:.4f}%`\nRota: `{' -> '.join(cycle_tuple)}`"
                        logging.info(msg)
                        asyncio.create_task(bot.send_message(CHAT_ID, msg, parse_mode="Markdown"))

                        if not state['dry_run']:
                            logging.info("MODO REAL: Executando negociação...")
                            await self._executar_trade_async(cycle_tuple, volume_da_rota)
                        else:
                            logging.info("MODO SIMULAÇÃO: Oportunidade não executada.")
                        
                        logging.info("Pausando por 60s após a oportunidade para estabilização do mercado.")
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
                logging.critical(f"❌ ERRO FATAL! O engine caiu. Reiniciando em 15 segundos...\nDetalhes: {e}\n\n{error_trace}")
                try:
                    await bot.send_message(CHAT_ID, f"🔴 **ERRO CRÍTICO! O engine caiu.**\nDetalhes: `{e}`\n\n```\n{error_trace}\n```\n\n**Tentando reiniciar o engine...**", parse_mode="Markdown")
                except Exception as alert_e:
                    logging.error(f"Falha ao enviar alerta de erro: {alert_e}")
                
                for task in list(self.websocket_tasks.values()):
                    if not task.done():
                        task.cancel()
                self.websocket_tasks.clear()
                self.order_books.clear()

                await asyncio.sleep(15)

async def main():
    """Função principal que inicia o bot e o loop de arbitragem."""
    try:
        logging.info("Iniciando bot v39.1 (Bot de Arbitragem)...")
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
        logging.info("Bibliotecas Telebot e CCXT inicializadas com sucesso.")
        
        # 2. Setup Log Handler
        telegram_handler = TelegramHandler(bot, CHAT_ID, asyncio.get_event_loop(), level=logging.CRITICAL)
        logging.getLogger().addHandler(telegram_handler)

        # 3. Setup Command Handlers
        setup_handlers(bot)

        # 4. Initialize Arbitrage Engine
        engine = ArbitrageEngine(exchange, asyncio.get_event_loop())

        logging.info("Bot e exchange inicializados. Iniciando tarefas de arbitragem e polling do Telegram.")
        
        # 5. Run the core tasks
        await asyncio.gather(
            engine.run_arbitrage_loop_outer(),
            bot.polling(none_stop=True)
        )
        
    except Exception as e:
        logging.critical(f"❌ Ocorreu um erro fatal durante a execução do bot: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
