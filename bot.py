# bot.py - v15.0 - Versão Final e Limpa

import os
import logging
import telebot
import ccxt
import time
from decimal import Decimal, getcontext, ROUND_DOWN
import threading
import random
import asyncio

# --- Configuração ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
getcontext().prec = 30

# --- Variáveis de Ambiente ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD")

# --- Inicialização ---
try:
    bot = telebot.TeleBot(TOKEN)
    exchange = ccxt.okx({
        'apiKey': OKX_API_KEY, 
        'secret': OKX_API_SECRET, 
        'password': OKX_API_PASSWORD,
        'options': {'defaultType': 'spot'}
    })
    exchange.load_markets()
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
    'min_profit': Decimal("0.001"),
    'volume_percent': Decimal("100.0"),
    'max_depth': 3,
    'stop_loss_usdt': None
}

# --- Parâmetros de Trade ---
TAXA_TAKER = Decimal("0.001")
MOEDAS_BASE_OPERACIONAIS = ['USDT', 'USDC']
MINIMO_ABSOLUTO_DO_VOLUME = Decimal("3.1")
MIN_ROUTE_DEPTH = 3
MARGEM_DE_SEGURANCA = Decimal("0.997")
FIAT_CURRENCIES = {'USD', 'EUR', 'GBP', 'JPY', 'BRL', 'AUD', 'CAD', 'CHF', 'CNY', 'HKD', 'SGD', 'KRW', 'INR', 'RUB', 'TRY', 'UAH', 'VND', 'THB', 'PHP', 'IDR', 'MYR', 'AED', 'SAR', 'ZAR', 'MXN', 'ARS', 'CLP', 'COP', 'PEN'}
BLACKLIST_MOEDAS = {'TON', 'SUI'}
ORDER_BOOK_DEPTH = 100

# --- Comandos do Bot ---
@bot.message_handler(commands=['start', 'ajuda'])
def send_welcome(message):
    bot.reply_to(message, "Bot v15.0 (Sniper de Arbitragem) online. Use /status.")

@bot.message_handler(commands=['saldo'])
def send_balance_command(message):
    try:
        bot.reply_to(message, "Buscando saldos na OKX...")
        balance = exchange.fetch_balance()
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
    stop_loss_text = f"{state['stop_loss_usdt']:.2f} USDT" if state['stop_loss_usdt'] else "Não definido"
    reply = (f"Status: {status_text}\n"
             f"Modo: **{mode_text}**\n"
             f"Lucro Mínimo: `{state['min_profit']:.4f}%`\n"
             f"Volume por Trade: `{state['volume_percent']:.2f}%`\n"
             f"Profundidade Máx. de Rotas: `{state['max_depth']}`\n"
             f"Stop Loss: `{stop_loss_text}`")
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

@bot.message_handler(commands=['setlucro', 'setvolume', 'setdepth', 'setstoploss'])
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
        elif command == 'setstoploss':
            if value.lower() == 'off':
                state['stop_loss_usdt'] = None
                bot.reply_to(message, "Stop loss desativado.")
            else:
                state['stop_loss_usdt'] = Decimal(value)
                bot.reply_to(message, f"Stop loss definido para {state['stop_loss_usdt']:.2f} USDT.")
        
        logging.info(f"Comando '{command} {value}' executado.")
    except Exception as e:
        bot.reply_to(message, f"Erro no comando. Uso: /{command} <valor>")
        logging.error(f"Erro ao processar comando '{message.text}': {e}")

@bot.message_handler(commands=['debug_radar'])
def debug_radar_command(message):
    try:
        bot.reply_to(message, "⚙️ Gerando relatório de simulação... Isso pode demorar um pouco.")
        
        balance = exchange.fetch_balance()
        
        volumes_a_usar = {}
        for moeda in MOEDAS_BASE_OPERACIONAIS:
            saldo_disponivel = Decimal(str(balance.get('free', {}).get(moeda, '0')))
            volumes_a_usar[moeda] = (saldo_disponivel * (state['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA
        
        melhores, piores = engine._simular_todas_as_rotas(volumes_a_usar)

        msg_melhores = "📊 **Radar de Depuração (Melhores Rotas Simuladas)**\n\n"
        if melhores:
            for i, res in enumerate(melhores, 1):
                arrow = "✅" if res['profit'] >= 0 else "🔽"
                msg_melhores += f"{i}. Rota: `{' -> '.join(res['cycle'])}`\n   Lucro Líquido Realista: `{arrow} {res['profit']:.4f}%`\n"
        else:
            msg_melhores += "Nenhuma rota lucrativa encontrada."
        
        msg_piores = "\n\n📉 **Radar de Depuração (Piores Rotas Simuladas)**\n\n"
        if piores:
            for i, res in enumerate(piores, 1):
                arrow = "✅" if res['profit'] >= 0 else "🔽"
                msg_piores += f"{i}. Rota: `{' -> '.join(res['cycle'])}`\n   Lucro Líquido Realista: `{arrow} {res['profit']:.4f}%`\n"
        else:
            msg_piores += "Nenhuma rota simulada com prejuízo."

        bot.send_message(message.chat.id, msg_melhores + msg_piores, parse_mode="Markdown")

    except Exception as e:
        bot.reply_to(message, f"❌ Erro ao gerar o relatório: {e}")
        logging.error(f"Erro no comando /debug_radar: {e}")

# --- Lógica de Arbitragem ---
class ArbitrageEngine:
    def __init__(self, exchange_instance):
        self.exchange = exchange_instance
        self.markets = self.exchange.markets
        self.graph = {}
        self.rotas_viaveis = []
        self.last_depth = state['max_depth']
        self.order_books = {}
        self.order_books_timestamp = 0
        self.lock = threading.Lock()

    def construir_rotas(self):
        logging.info("Construindo mapa de rotas...")
        with self.lock:
            self.graph = {}
            self.rotas_viaveis = []
            self.order_books = {}

        active_markets = {
            s: m for s, m in self.markets.items() 
            if m.get('active') 
            and m.get('base') and m.get('quote') 
            and m['base'] not in FIAT_CURRENCIES and m['quote'] not in FIAT_CURRENCIES
            and m['base'] not in BLACKLIST_MOEDAS and m['quote'] not in BLACKLIST_MOEDAS
        }
        
        # REMOVIDO: A verificação de `fetch_trading_limits` que estava causando o erro
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
            random.shuffle(self.rotas_viaveis)
            self.last_depth = state['max_depth']
        logging.info(f"Mapa de rotas reconstruído para profundidade {self.last_depth}. {len(self.rotas_viaveis)} rotas encontradas.")
        bot.send_message(CHAT_ID, f"🗺️ Mapa de rotas reconstruído para profundidade {self.last_depth}. {len(self.rotas_viaveis)} rotas encontradas.")

    def _get_pair_details(self, coin_from, coin_to):
        pair_v1 = f"{coin_from}/{coin_to}"
        if pair_v1 in self.markets: return pair_v1, 'sell'
        pair_v2 = f"{coin_to}/{coin_from}"
        if pair_v2 in self.markets: return pair_v2, 'buy'
        return None, None

    def _simular_trade_com_slippage(self, cycle_path, investimento_inicial, order_books_cache):
        try:
            valor_simulado = investimento_inicial
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
                else: # side == 'sell'
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
            logging.error(f"Erro na simulação para a rota {' -> '.join(cycle_path)}: {e}", exc_info=True)
            return None

    def _simular_todas_as_rotas(self, volumes_iniciais):
        with self.lock:
            self._fetch_all_order_books()
        
            if not self.order_books:
                logging.warning("Não há livros de ordens para análise. Abortando simulação.")
                return [], []
            
            resultados = []
            for cycle_tuple in self.rotas_viaveis:
                resultado = self._simular_trade_com_slippage(list(cycle_tuple), volumes_iniciais.get(cycle_tuple[0], Decimal('0')), self.order_books)
                if resultado is not None:
                    resultados.append({'cycle': cycle_tuple, 'profit': resultado})

            resultados.sort(key=lambda x: x['profit'], reverse=True)
            melhores = resultados[:10]
            piores = resultados[-10:]
        
        return melhores, piores

    def _fetch_all_order_books(self):
        logging.info("Buscando livros de ordens para todas as rotas...")
        all_pairs = set()
        with self.lock:
            for route in self.rotas_viaveis:
                for i in range(len(route) - 1):
                    pair_id, _ = self._get_pair_details(route[i], route[i+1])
                    if pair_id:
                        all_pairs.add(pair_id)

        new_order_books = {}
        for pair in all_pairs:
            try:
                ob = self.exchange.fetch_order_book(pair, limit=ORDER_BOOK_DEPTH)
                new_order_books[pair] = ob
            except Exception as e:
                logging.error(f"Erro ao buscar order book para {pair}: {e}")
        
        if new_order_books:
            with self.lock:
                self.order_books = new_order_books
                self.order_books_timestamp = time.time()
            logging.info(f"Livros de ordens atualizados para {len(new_order_books)} pares.")
        else:
            logging.warning("Não foi possível atualizar nenhum livro de ordens.")

    def _formatar_erro_telegram(self, leg_error, perna, rota):
        erro_str = str(leg_error)
        detalhes = f"Falha na Perna {perna} da Rota: `{' -> '.join(rota)}`\n"
        
        if isinstance(leg_error, ccxt.ExchangeError):
            try:
                erro_json_str = erro_str.split('okx ')[1].split('}')[0] + '}'
                erro_json = eval(erro_json_str)
                detalhes += f"Código de Erro OKX: `{erro_json.get('sCode', 'N/A')}`\n"
                detalhes += f"Mensagem de Erro: `{erro_json.get('sMsg', 'N/A')}`"
            except Exception:
                detalhes += f"Detalhes do Erro: `{erro_str}`"
        else:
            detalhes += f"Detalhes do Erro: `{erro_str}`"
            
        return detalhes

    def _executar_trade(self, cycle_path, volume_a_usar):
        base_moeda = cycle_path[0]
        bot.send_message(CHAT_ID, f"🚀 **MODO REAL** 🚀\nIniciando execução da rota: `{' -> '.join(cycle_path)}`\nVolume: `{volume_a_usar:.2f} {base_moeda}`", parse_mode="Markdown")
        
        moedas_presas = []
        current_asset = base_moeda
        
        live_balance = self.exchange.fetch_balance()
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
                    ticker = self.exchange.fetch_ticker(pair_id)
                    price_to_use = Decimal(str(ticker['ask']))
                    
                    if price_to_use == 0:
                        raise Exception(f"Preço de 'ask' inválido (zero) para o par {pair_id}.")

                    amount_to_buy = current_amount / price_to_use
                    
                    trade_volume_precisao = self.exchange.amount_to_precision(pair_id, float(amount_to_buy))
                    
                    logging.info(f"DEBUG: Tentando comprar {trade_volume_precisao} {coin_to} com {current_amount} {coin_from} no par {pair_id}")
                    
                    order = self.exchange.create_market_buy_order(pair_id, trade_volume_precisao)

                else: # side == 'sell'
                    trade_volume = self.exchange.amount_to_precision(pair_id, float(current_amount))
                    logging.info(f"DEBUG: Tentando vender com {trade_volume} {coin_from} para {coin_to} no par {pair_id}")
                    order = self.exchange.create_market_sell_order(pair_id, trade_volume)
                
                time.sleep(2.5) 
                order_status = self.exchange.fetch_order(order['id'], pair_id)

                if order_status['status'] != 'closed':
                    raise Exception(f"Ordem {order['id']} não foi completamente preenchida. Status: {order_status['status']}")
                
                live_balance = self.exchange.fetch_balance()
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
                        time.sleep(5)
                        live_balance = self.exchange.fetch_balance()
                        ativo_amount = Decimal(str(live_balance.get(ativo_symbol, {}).get('free', '0')))
                        
                        if ativo_amount == 0:
                            raise Exception("Saldo real do ativo preso é zero. Não é possível resgatar.")
                            
                        reversal_pair, reversal_side = self._get_pair_details(ativo_symbol, base_moeda)
                        if not reversal_pair:
                            raise Exception(f"Par de reversão {ativo_symbol}/{base_moeda} não encontrado.")

                        if reversal_side == 'buy':
                            reversal_amount = self.exchange.amount_to_precision(reversal_pair, float(ativo_amount))
                            self.exchange.create_market_buy_order(reversal_pair, reversal_amount)
                        else:
                            reversal_amount = self.exchange.amount_to_precision(reversal_pair, float(ativo_amount))
                            self.exchange.create_market_sell_order(reversal_pair, reversal_amount)
                            
                        bot.send_message(CHAT_ID, f"✅ **Venda de Emergência EXECUTADA!** Resgatado: `{Decimal(str(reversal_amount)):.8f} {ativo_symbol}`", parse_mode="Markdown")
                        
                    except Exception as reversal_error:
                        bot.send_message(CHAT_ID, f"❌ **FALHA CRÍTICA NA VENDA DE EMERGÊNCIA:** `{reversal_error}`. **VERIFIQUE A CONTA MANUALMENTE!**", parse_mode="Markdown")
                return
        
        lucro_real_usdt = current_amount - volume_a_usar
        lucro_real_percent = (lucro_real_usdt / volume_a_usar) * 100
        bot.send_message(CHAT_ID, f"✅ **SUCESSO!**\nRota Concluída: `{' -> '.join(cycle_path)}`\nLucro: `{lucro_real_usdt:.4f} {base_moeda}` (`{lucro_real_percent:.4f}%`)")

    def main_loop(self):
        self.construir_rotas()
        ciclo_num = 0
        while True:
            try:
                if self.last_depth != state['max_depth']:
                    self.construir_rotas()

                if not state['is_running']:
                    time.sleep(10)
                    continue

                balance = self.exchange.fetch_balance()
                
                if state['stop_loss_usdt']:
                    saldo_total_usdt = Decimal(str(balance.get('total', {}).get('USDT', '0')))
                    if saldo_total_usdt < state['stop_loss_usdt']:
                        state['is_running'] = False
                        logging.warning(f"STOP-LOSS ATINGIDO! Saldo {saldo_total_usdt:.2f} USDT < {state['stop_loss_usdt']:.2f} USDT. Operações pausadas.")
                        bot.send_message(CHAT_ID, f"🚨 **STOP-LOSS ATINGIDO!** 🚨\nSaldo atual: `{saldo_total_usdt:.2f} USDT`\nLimite: `{state['stop_loss_usdt']:.2f} USDT`\n**O motor foi pausado automaticamente.**", parse_mode="Markdown")
                        state['stop_loss_usdt'] = None
                        continue

                ciclo_num += 1
                logging.info(f"--- Iniciando Ciclo #{ciclo_num} | Modo: {'Simulação' if state['dry_run'] else '⚠️ REAL ⚠️'} | Lucro Mín: {state['min_profit']}% ---")
                
                volumes_a_usar = {}
                for moeda in MOEDAS_BASE_OPERACIONAIS:
                    saldo_disponivel = Decimal(str(balance.get('free', {}).get(moeda, '0')))
                    volumes_a_usar[moeda] = (saldo_disponivel * (state['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA

                with self.lock:
                    self._fetch_all_order_books()
                
                if not self.order_books:
                    logging.warning("Não há livros de ordens para análise. Aguardando...")
                    time.sleep(5)
                    continue
                
                for i, cycle_tuple in enumerate(self.rotas_viaveis):
                    if not state['is_running']: break
                    if i > 0 and i % 250 == 0: logging.info(f"Analisando rota {i}/{len(self.rotas_viaveis)}...")

                    base_moeda_da_rota = cycle_tuple[0]
                    volume_da_rota = volumes_a_usar.get(base_moeda_da_rota, Decimal('0'))

                    if volume_da_rota < MINIMO_ABSOLUTO_DO_VOLUME:
                        continue

                    with self.lock:
                        resultado = self._simular_trade_com_slippage(list(cycle_tuple), volume_da_rota, self.order_books)
                    
                    if resultado is not None and resultado > state['min_profit']:
                        msg = f"✅ **OPORTUNIDADE**\nLucro: `{resultado:.4f}%`\nRota: `{' -> '.join(cycle_tuple)}`"
                        logging.info(msg)
                        bot.send_message(CHAT_ID, msg, parse_mode="Markdown")
                        
                        if not state['dry_run']:
                            self._executar_trade(cycle_tuple, volume_da_rota)
                        else:
                            logging.info("MODO SIMULAÇÃO: Oportunidade não executada.")
                        
                        logging.info("Pausa de 60s após oportunidade para estabilização do mercado.")
                        time.sleep(60)
                        break
                
                logging.info(f"Ciclo #{ciclo_num} concluído. Aguardando 10 segundos.")
                time.sleep(10)

            except Exception as e:
                logging.critical(f"Erro CRÍTICO no ciclo de análise: {e}")
                bot.send_message(CHAT_ID, f"🔴 **Erro Crítico no Motor** 🔴\n`{e}`\nO bot tentará novamente em 60 segundos.")
                time.sleep(60)

# --- Iniciar Tudo ---
if __name__ == "__main__":
    logging.info("Iniciando o bot v15.0 (Sniper de Arbitragem)...")
    
    engine = ArbitrageEngine(exchange)
    
    engine_thread = threading.Thread(target=engine.main_loop)
    engine_thread.daemon = True
    engine_thread.start()
    
    logging.info("Motor rodando em uma thread. Iniciando polling do Telebot...")
    try:
        bot.send_message(CHAT_ID, "✅ **Bot Gênesis v15.0 (Sniper de Arbitragem) iniciado com sucesso!**")
        bot.polling(non_stop=True)
    except Exception as e:
        logging.critical(f"Não foi possível iniciar o polling do Telegram ou enviar mensagem inicial: {e}")
