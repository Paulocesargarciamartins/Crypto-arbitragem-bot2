import os
import logging
import telebot
import ccxt.pro as ccxt
from decimal import Decimal, getcontext, ROUND_DOWN
import threading
import traceback
import asyncio
import time
from datetime import datetime

# --- Configuração Global ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
getcontext().prec = 30

# --- Variáveis de Ambiente ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD")

# --- Parâmetros de Trade ---
TAXA_TAKER = Decimal("0.001")
MOEDAS_BASE_OPERACIONAIS = ['USDT', 'USDC']
MINIMO_ABSOLUTO_DO_VOLUME = Decimal("3.1")
MIN_ROUTE_DEPTH = 3
MARGEM_DE_SEGURANCA = Decimal("0.997")
FIAT_CURRENCIES = {'USD', 'EUR', 'GBP', 'JPY', 'BRL', 'AUD', 'CAD', 'CHF', 'CNY', 'HKD', 'SGD', 'KRW', 'INR', 'RUB', 'TRY', 'UAH', 'VND', 'THB', 'PHP', 'IDR', 'MYR', 'AED', 'SAR', 'ZAR', 'MXN', 'ARS', 'CLP', 'COP', 'PEN'}
BLACKLIST_MOEDAS = {'TON', 'SUI'}
ORDER_BOOK_DEPTH = 100
API_TIMEOUT_SECONDS = 60 # Aumentado para 60 segundos para evitar timeouts frequentes
VERBOSE_ERROR_LOGGING = False # Mude para True se quiser ver todas as mensagens de timeout

# --- Configuração do Stop Loss (mantido da versão anterior) ---
STOP_LOSS_LEVEL_1_PERCENT = Decimal("-0.5")
STOP_LOSS_LEVEL_2_PERCENT = Decimal("-1.0")

# --- Handlers de Log ---
class TelegramHandler(logging.Handler):
    def __init__(self, bot_instance, chat_id, level=logging.CRITICAL):
        super().__init__(level)
        self.bot = bot_instance
        self.chat_id = chat_id
        self.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))

    def emit(self, record):
        log_entry = self.format(record)
        try:
            self.bot.send_message(self.chat_id, f"🔴 **ERRO CRÍTICO NO BOT!**\n\n`{log_entry}`", parse_mode="Markdown")
        except Exception as e:
            # Fallback para o console se o Telegram falhar
            print(f"Falha ao enviar log para o Telegram: {e}")

# --- Inicialização ---
try:
    bot = telebot.TeleBot(TOKEN)
    
    # Configurar o handler de log para o Telegram
    telegram_handler = TelegramHandler(bot, CHAT_ID, level=logging.CRITICAL)
    logging.getLogger().addHandler(telegram_handler)

    exchange = ccxt.okx({
        'apiKey': OKX_API_KEY,
        'secret': OKX_API_SECRET,
        'password': OKX_API_PASSWORD,
        'options': {'defaultType': 'spot'},
        'timeout': API_TIMEOUT_SECONDS * 1000
    })
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    loop.run_until_complete(exchange.load_markets())
    logging.info("Bibliotecas Telebot e CCXT iniciadas com sucesso.")
except Exception as e:
    logging.critical(f"Falha ao iniciar bibliotecas: {e}")
    if bot and CHAT_ID:
        try:
            bot.send_message(CHAT_ID, f"ERRO CRÍTICO NA INICIALIZAÇÃO: {e}. O bot não pode iniciar.")
        except Exception as alert_e:
            logging.error(f"Falha ao enviar alerta de erro: {alert_e}")
    exit()

# --- Estado do Bot ---
state = {
    'is_running': True,
    'dry_run': True,
    'min_profit': Decimal("0.005"),
    'volume_percent': Decimal("100.0"),
    'max_depth': 3,
    'stop_loss_usdt': None
}

# --- Comandos do Bot ---
@bot.message_handler(commands=['start', 'ajuda'])
def send_welcome(message):
    bot.reply_to(message, "Bot v26.0 (Bot de Arbitragem) online. Use /status.")

@bot.message_handler(commands=['saldo'])
def send_balance_command(message):
    try:
        bot.reply_to(message, "Buscando saldos na OKX...")
        balance = loop.run_until_complete(exchange.fetch_balance())
        reply = "📊 **Saldos (OKX):**\n"
        for moeda in MOEDAS_BASE_OPERACIONAIS:
            saldo = balance.get(moeda, {'free': 0, 'total': 0})
            saldo_livre = Decimal(str(saldo.get('free', '0')))
            saldo_total = Decimal(str(saldo.get('total', '0')))
            reply += (f"- `{moeda}`\n"
                      f"  Disponível para Trade: `{saldo_livre:.4f}`\n"
                      f"  Total (incl. em ordens): `{saldo_total:.4f}`\n")
        
        bot.send_message(message.chat.id, reply, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Erro ao buscar saldos: {e}")
        logging.error(f"Erro no comando /saldo: {e}")

@bot.message_handler(commands=['status'])
def send_status(message):
    status_text = "Em operação" if state['is_running'] else "Pausado"
    mode_text = "Simulação" if state['dry_run'] else "⚠️ MODO REAL ⚠️"
    stop_loss_text = "Não definido"
    reply = (f"Status: {status_text}\n"
             f"Modo: **{mode_text}**\n"
             f"Lucro Mínimo: `{state['min_profit']:.4f}%`\n"
             f"Volume por Trade: `{state['volume_percent']:.2f}%`\n"
             f"Profundidade Máx. de Rotas: `{state['max_depth']}`")
    bot.send_message(message.chat.id, reply, parse_mode="Markdown")

@bot.message_handler(commands=['pausar', 'retomar', 'modo_real', 'modo_simulacao'])
def simple_commands(message):
    command = message.text.split('@')[0][1:]
    if command == 'pausar':
        state['is_running'] = False
        bot.reply_to(message, "Motor pausado.")
    elif command == 'retomar':
        state['is_running'] = True
        bot.reply_to(message, "Motor retomado.")
    elif command == 'modo_real':
        state['dry_run'] = False
        bot.reply_to(message, "⚠️ MODO REAL ATIVADO! ⚠️ As próximas oportunidades serão executadas.")
    elif command == 'modo_simulacao':
        state['dry_run'] = True
        bot.reply_to(message, "Modo Simulação ativado.")
    logging.info(f"Comando '{command}' executado.")

@bot.message_handler(commands=['setlucro', 'setvolume', 'setdepth'])
def value_commands(message):
    try:
        parts = message.text.split(maxsplit=1)
        command = parts[0].split('@')[0][1:]
        value = parts[1] if len(parts) > 1 else ""

        if command == 'setlucro':
            state['min_profit'] = Decimal(value)
            bot.reply_to(message, f"Lucro mínimo definido para {state['min_profit']:.4f}%")
        elif command == 'setvolume':
            vol = Decimal(value)
            if 0 < vol <= 100:
                state['volume_percent'] = vol
                bot.reply_to(message, f"Volume de trade definido para {state['volume_percent']:.2f}%")
            else:
                bot.reply_to(message, "Volume deve ser entre 1 e 100.")
        elif command == 'setdepth':
            depth = int(value)
            if MIN_ROUTE_DEPTH <= depth <= 5:
                state['max_depth'] = depth
                bot.reply_to(message, f"Profundidade de rotas definida para {state['max_depth']}. O mapa será reconstruído no próximo ciclo.")
            else:
                bot.reply_to(message, f"Profundidade deve ser entre {MIN_ROUTE_DEPTH} e 5.")
        
        logging.info(f"Comando '{command} {value}' executado.")
    except Exception as e:
        bot.reply_to(message, f"Erro no comando. Uso: /{command} <valor>")
        logging.error(f"Erro ao processar comando '{message.text}': {e}")
        
@bot.message_handler(commands=['verificar_ws'])
def check_websocket_status(message):
    try:
        start_time = time.time()
        timeout = 60  # Tempo máximo de espera em segundos
        
        # Espera até que pelo menos um livro de ofertas seja recebido
        while not engine.order_books and (time.time() - start_time) < timeout:
            time.sleep(1)
        
        if not engine.order_books:
            bot.reply_to(message, "❌ **O motor não iniciou o monitoramento de livros de ordens.** Verifique se o bot está rodando e se há rotas válidas.")
            return

        report = "🔍 **Status das Conexões WebSocket**\n"
        current_time = datetime.now()
        for symbol, orderbook in engine.order_books.items():
            if 'timestamp' in orderbook:
                last_update_ms = orderbook['timestamp']
                last_update_s = last_update_ms / 1000.0
                last_update_dt = datetime.fromtimestamp(last_update_s)
                time_diff_s = (current_time - last_update_dt).total_seconds()
                
                status_emoji = "✅" if time_diff_s < 10 else "⚠️"
                report += f"{status_emoji} `{symbol}` - Última atualização: `{time_diff_s:.2f}s` atrás.\n"
            else:
                report += f"❓ `{symbol}` - Sem dados de timestamp.\n"
        
        bot.send_message(message.chat.id, report, parse_mode="Markdown")

    except Exception as e:
        bot.reply_to(message, f"❌ Erro ao verificar status dos WebSockets: {e}")
        logging.error(f"Erro no comando /verificar_ws: {e}")

# --- Lógica de Arbitragem ---
class ArbitrageEngine:
    def __init__(self, exchange_instance, event_loop):
        self.exchange = exchange_instance
        self.markets = self.exchange.markets
        self.loop = event_loop
        self.graph = {}
        self.rotas_viaveis = []
        self.last_depth = state['max_depth']
        self.order_books = {}
        self.lock = threading.Lock()
        self.timeout_counters = {} # Contador de timeouts por par
        self._shutdown_event = asyncio.Event()

    def construir_rotas(self):
        logging.info("Construindo mapa de rotas...")
        with self.lock:
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

        with self.lock:
            self.rotas_viaveis = [tuple(rota) for rota in todas_as_rotas]
            self.last_depth = state['max_depth']
        logging.info(f"Mapa de rotas reconstruído para profundidade {self.last_depth}. {len(self.rotas_viaveis)} rotas encontradas.")
        bot.send_message(CHAT_ID, f"🗺️ Mapa de rotas reconstruído para profundidade {self.last_depth}. {len(self.rotas_viaveis)} rotas encontradas.")

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
            raise Exception(f"Erro na simulação para a rota {' -> '.join(cycle_path)}: {e}")

    async def _executar_trade_async(self, cycle_path, volume_a_usar):
        base_moeda = cycle_path[0]
        bot.send_message(CHAT_ID, f"🚀 **MODO REAL** 🚀\nIniciando execução da rota: `{' -> '.join(cycle_path)}`\nInvestimento Planejado: `{volume_a_usar:.8f} {base_moeda}`", parse_mode="Markdown")

        moedas_presas = []
        current_asset = base_moeda

        live_balance = await self.exchange.fetch_balance()
        current_amount = Decimal(str(live_balance.get(current_asset, {}).get('free', '0'))) * MARGEM_DE_SEGURANCA
        if current_amount < MINIMO_ABSOLUTO_DO_VOLUME:
            bot.send_message(CHAT_ID, f"❌ **FALHA NA ROTA!** Saldo de `{current_amount:.2f} {current_asset}` está abaixo do mínimo para trade (`{MINIMO_ABSOLUTO_DO_VOLUME:.2f} {current_asset}`).", parse_mode="Markdown")
            return

        for i in range(len(cycle_path) - 1):
            coin_from, coin_to = cycle_path[i], cycle_path[i+1]

            try:
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                if not pair_id: raise Exception(f"Par inválido {coin_from}/{coin_to}")

                if side == 'buy':
                    ticker = await self.exchange.fetch_ticker(pair_id)
                    price_to_use = Decimal(str(ticker['ask']))

                    if price_to_use == 0:
                        raise Exception(f"Preço de 'ask' inválido (zero) para o par {pair_id}.")

                    amount_to_buy = current_amount / price_to_use
                    trade_volume_precisao = self.exchange.amount_to_precision(pair_id, float(amount_to_buy))

                    logging.info(f"DEBUG: Tentando comprar {trade_volume_precisao} {coin_to} com {current_amount} {coin_from} no par {pair_id}")
                    bot.send_message(CHAT_ID, f"⏳ Passo {i+1}/{len(cycle_path)-1}: Negociando {current_amount:.4f} {coin_from} para {coin_to} no par {pair_id.replace('/', '_')}.")

                    order = await self.exchange.create_market_buy_order(pair_id, trade_volume_precisao)

                else:
                    trade_volume = self.exchange.amount_to_precision(pair_id, float(current_amount))
                    logging.info(f"DEBUG: Tentando vender com {trade_volume} {coin_from} para {coin_to} no par {pair_id}")
                    bot.send_message(CHAT_ID, f"⏳ Passo {i+1}/{len(cycle_path)-1}: Negociando {current_amount:.4f} {coin_from} para {coin_to} no par {pair_id.replace('/', '_')}.")

                    order = await self.exchange.create_market_sell_order(pair_id, trade_volume)

                await asyncio.sleep(2.5)
                order_status = await self.exchange.fetch_order(order['id'], pair_id)

                if order_status['status'] != 'closed':
                    raise Exception(f"Ordem {order['id']} não foi completamente preenchida. Status: {order_status['status']}")

                live_balance = await self.exchange.fetch_balance()
                current_amount = Decimal(str(live_balance.get(coin_to, {}).get('free', '0')))
                current_asset = coin_to

                moedas_presas.append({'symbol': current_asset, 'amount': current_amount})

            except Exception as leg_error:
                logging.critical(f"FALHA NA PERNA {i+1} ({coin_from}->{coin_to}): {leg_error}")
                mensagem_detalhada = self._formatar_erro_telegram(leg_error, i + 1, cycle_path)
                bot.send_message(CHAT_ID, f"🔴 **FALHA NA ROTA!**\n{mensagem_detalhada}", parse_mode="Markdown")

                if moedas_presas:
                    ativo_preso_details = moedas_presas[-1]
                    ativo_symbol = ativo_preso_details['symbol']

                    bot.send_message(CHAT_ID, f"⚠️ **CAPITAL PRESO!**\nAtivo: `{ativo_symbol}`.\n**Iniciando venda de emergência para {base_moeda}...**", parse_mode="Markdown")

                    try:
                        await asyncio.sleep(5)
                        live_balance = await self.exchange.fetch_balance()
                        ativo_amount = Decimal(str(live_balance.get(ativo_symbol, {}).get('free', '0')))

                        if ativo_amount == 0:
                            raise Exception("Saldo real do ativo preso é zero. Não é possível resgatar.")

                        reversal_pair, reversal_side = self._get_pair_details(ativo_symbol, base_moeda)
                        if not reversal_pair:
                            raise Exception(f"Par de reversão {ativo_symbol}/{base_moeda} não encontrado.")

                        if reversal_side == 'buy':
                            reversal_amount = self.exchange.amount_to_precision(reversal_pair, float(ativo_amount))
                            await self.exchange.create_market_buy_order(reversal_pair, reversal_amount)
                        else:
                            reversal_amount = self.exchange.amount_to_precision(reversal_pair, float(ativo_amount))
                            await self.exchange.create_market_sell_order(reversal_pair, reversal_amount)

                        bot.send_message(CHAT_ID, f"✅ **Venda de Emergência EXECUTADA!** Resgatado: `{Decimal(str(reversal_amount)):.8f} {ativo_symbol}`", parse_mode="Markdown")

                    except Exception as reversal_error:
                        bot.send_message(CHAT_ID, f"❌ **FALHA CRÍTICA NA VENDA DE EMERGÊNCIA:** `{reversal_error}`. **VERIFIQUE A CONTA MANUALMENTE!**", parse_mode="Markdown")
                return

        live_balance_final = await self.exchange.fetch_balance()
        final_amount = Decimal(str(live_balance_final.get(base_moeda, {}).get('free', '0')))

        lucro_real_usdt = final_amount - volume_a_usar
        if volume_a_usar == 0: lucro_real_percent = Decimal('0')
        else: lucro_real_percent = (lucro_real_usdt / volume_a_usar) * 100

        bot.send_message(CHAT_ID, f"✅ **SUCESSO! Rota Concluída.**\nRota: `{' -> '.join(cycle_path)}`\nLucro: `{lucro_real_usdt:.4f} {base_moeda}` (`{lucro_real_percent:.4f}%`)", parse_mode="Markdown")

    def _proactive_diagnostics(self):
        logging.info("Iniciando verificação de diagnóstico proativo ('Radar')...")
        issues_found = []
        try:
            markets_data = self.loop.run_until_complete(self.exchange.load_markets())
            if not markets_data:
                issues_found.append("Falha ao carregar dados de mercados.")
            else:
                for symbol in markets_data:
                    market = markets_data[symbol]
                    if market.get('active', False):
                        if market.get('base') and market.get('quote'):
                            if market.get('info', {}).get('minSz') == '0':
                                issues_found.append(f"Par {symbol} tem tamanho mínimo de 0, pode causar erro.")
                        else:
                            issues_found.append(f"Par {symbol} não tem base/quote definidos.")
            
            if not issues_found:
                logging.info("✅ Radar de diagnóstico proativo concluído. Nenhuma anomalia crítica encontrada.")
                bot.send_message(CHAT_ID, "✅ **Radar de Diagnóstico Ativo**\nNenhuma anomalia crítica encontrada.", parse_mode="Markdown")
            else:
                log_msg = f"⚠️ O Radar de diagnóstico encontrou {len(issues_found)} anomalias:\n" + "\n".join(issues_found)
                logging.warning(log_msg)
                bot.send_message(CHAT_ID, f"⚠️ **Radar de Diagnóstico Ativo!**\n`{log_msg}`", parse_mode="Markdown")

        except Exception as e:
            issues_found.append(f"Erro ao executar o diagnóstico: {e}")
            logging.error(f"Erro no diagnóstico proativo: {e}")
            bot.send_message(CHAT_ID, f"❌ **Falha no Radar de Diagnóstico!**\nErro: `{e}`", parse_mode="Markdown")

    @bot.message_handler(commands=['diagnostico'])
    def trigger_diagnostics(message):
        try:
            engine._proactive_diagnostics()
            bot.reply_to(message, "Executando o diagnóstico. Os resultados serão enviados para o chat.")
        except NameError:
            bot.reply_to(message, "O motor de arbitragem não está inicializado. Tente reiniciar o bot.")

    async def run_arbitrage_loop(self):
        try:
            logging.info("Iniciando loop principal de arbitragem...")
            self.construir_rotas()
            logging.info("Rotas construídas, coletando pares para monitoramento...")
            
            pares_a_monitorar = set()
            for rota in self.rotas_viaveis:
                for i in range(len(rota) - 1):
                    pair_id, _ = self._get_pair_details(rota[i], rota[i+1])
                    if pair_id:
                        pares_a_monitorar.add(pair_id)

            logging.info(f"Identificados {len(pares_a_monitorar)} pares para monitorar. Criando tarefas...")
            tasks = {pair: asyncio.create_task(self.watch_order_book(pair)) for pair in pares_a_monitorar}
            
            logging.info("Tarefas de monitoramento criadas com sucesso. Entrando no loop de verificação.")
            
            while True:
                # Checa o estado do bot antes de prosseguir com qualquer lógica
                if not state['is_running']:
                    logging.info("Bot pausado. Aguardando comando para retomar...")
                    while not state['is_running']:
                        await asyncio.sleep(1) # Espera 1 segundo e checa novamente
                    logging.info("Bot retomado. Continuar operação.")
                
                try:
                    volumes_a_usar = {}
                    balance = await self.exchange.fetch_balance()
                    for moeda in MOEDAS_BASE_OPERACIONAIS:
                        saldo_disponivel = Decimal(str(balance.get(moeda, {}).get('free', '0')))
                        volumes_a_usar[moeda] = (saldo_disponivel * (state['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA
                    
                    if self.last_depth != state['max_depth']:
                        self.construir_rotas()
                        new_pares = set()
                        for rota in self.rotas_viaveis:
                            for i in range(len(rota) - 1):
                                pair_id, _ = self._get_pair_details(rota[i], rota[i+1])
                                if pair_id:
                                    new_pares.add(pair_id)

                        pares_a_remover = tasks.keys() - new_pares
                        for pair in pares_a_remover:
                            tasks[pair].cancel()
                            del tasks[pair]

                        for pair in new_pares - tasks.keys():
                            tasks[pair] = asyncio.create_task(self.watch_order_book(pair))

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
                                bot.send_message(CHAT_ID, msg, parse_mode="Markdown")

                                if not state['dry_run']:
                                    logging.info("MODO REAL: Executando trade...")
                                    await self._executar_trade_async(cycle_tuple, volume_da_rota)
                                else:
                                    logging.info("MODO SIMULAÇÃO: Oportunidade não executada.")
                                
                                logging.info("Pausa de 60s após oportunidade para estabilização do mercado.")
                                await asyncio.sleep(60)
                                break
                    
                except Exception as e:
                    error_trace = traceback.format_exc()
                    logging.critical(f"❌ ERRO CRÍTICO NO LOOP DE ARBITRAGEM! {e}\n{error_trace}")
                    error_msg = f"🔴 **ERRO CRÍTICO NO BOT!**\n\n**O bot pode ter parado de funcionar.**\n\n**Detalhes do Erro:**\n`{e}`\n\n**Rastreamento Completo:**\n```\n{error_trace}\n```"
                    try:
                        bot.send_message(CHAT_ID, error_msg, parse_mode="Markdown")
                    except Exception as alert_e:
                        logging.error(f"Falha ao enviar alerta de erro para o Telegram: {alert_e}")
                    await asyncio.sleep(60)

        except Exception as loop_error:
            # Esta parte captura o erro na inicialização do loop principal
            error_trace = traceback.format_exc()
            logging.critical(f"❌ ERRO FATAL! Falha na inicialização do loop principal: {loop_error}\n{error_trace}")
            try:
                bot.send_message(CHAT_ID, f"🔴 **ERRO CRÍTICO! O motor não pôde iniciar.**\nDetalhes: `{loop_error}`\n\n```\n{error_trace}\n```", parse_mode="Markdown")
            except Exception as alert_e:
                logging.error(f"Falha ao enviar alerta de erro: {alert_e}")
            
    async def watch_order_book(self, symbol):
        # Inicializa o contador de timeouts para o par
        self.timeout_counters[symbol] = 0
        while True:
            try:
                orderbook = await asyncio.wait_for(self.exchange.watch_order_book(symbol), timeout=API_TIMEOUT_SECONDS)
                with self.lock:
                    self.order_books[symbol] = orderbook
                
                # Se a conexão foi bem-sucedida, reseta o contador
                self.timeout_counters[symbol] = 0

            except asyncio.TimeoutError:
                self.timeout_counters[symbol] += 1
                
                if VERBOSE_ERROR_LOGGING:
                    # Envia um alerta para cada timeout
                    logging.critical(f"❌ ERRO: Timeout ao tentar conectar ao par {symbol}. Tentando novamente...")
                else:
                    # Envia um alerta apenas após 10 falhas consecutivas
                    if self.timeout_counters[symbol] >= 10:
                        logging.critical(f"❌ ERRO PERSISTENTE: O par {symbol} falhou 10 vezes seguidas. Verifique sua conexão ou a API da OKX.")
                        self.timeout_counters[symbol] = 0  # Reseta o contador para evitar spam
                    else:
                        logging.warning(f"⚠️ Aviso: Timeout para o par {symbol}. Tentativa {self.timeout_counters[symbol]}/10...")
                
                await asyncio.sleep(5) # Espera para não sobrecarregar
            except Exception as e:
                logging.critical(f"❌ ERRO GRAVE ao monitorar o livro de ordens de {symbol}: {e}. O bot irá tentar reconectar em 5 segundos.")
                await asyncio.sleep(5)

# --- Iniciar Tudo ---
if __name__ == "__main__":
    logging.info("Iniciando o bot v26.0 (Bot de Arbitragem)...")
    
    new_loop = asyncio.new_event_loop()
    
    engine = ArbitrageEngine(exchange, new_loop)
    
    def start_engine_loop():
        asyncio.set_event_loop(new_loop)
        new_loop.run_until_complete(engine.run_arbitrage_loop())

    engine_thread = threading.Thread(target=start_engine_loop)
    engine_thread.daemon = True
    engine_thread.start()
    
    logging.info("Motor rodando em uma thread. Iniciando polling do Telebot...")
    try:
        bot.send_message(CHAT_ID, "✅ **Bot Gênesis v26.0 (Bot de Arbitragem) iniciado com sucesso!**")
        bot.polling(none_stop=True)
    except Exception as e:
        logging.critical(f"Não foi possível iniciar o polling do Telegram ou enviar mensagem inicial: {e}")
