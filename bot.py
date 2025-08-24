# bot.py - v11.2 - Comando /saldo adicionado

import os
import logging
import telebot
import ccxt
import time
from decimal import Decimal, getcontext
import threading
import random

# --- Configuração (sem alterações) ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
getcontext().prec = 30
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD")

# --- Inicialização (sem alterações) ---
try:
    bot = telebot.TeleBot(TOKEN)
    exchange = ccxt.okx({'apiKey': OKX_API_KEY, 'secret': OKX_API_SECRET, 'password': OKX_API_PASSWORD})
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

# --- Estado do Bot (sem alterações) ---
state = {
    'is_running': True,
    'dry_run': True,
    'min_profit': Decimal("0.4"),
    'volume_percent': Decimal("100.0"),
    'max_depth': 3,
    'stop_loss_usdt': None
}
# --- Parâmetros de Trade (sem alterações) ---
TAXA_TAKER = Decimal("0.001")
MOEDA_BASE_OPERACIONAL = 'USDT'
MINIMO_ABSOLUTO_USDT = Decimal("3.1")
MIN_ROUTE_DEPTH = 3
MARGEM_DE_SEGURANCA = Decimal("0.995")
FIAT_CURRENCIES = {'USD', 'EUR', 'GBP', 'JPY', 'BRL', 'AUD', 'CAD', 'CHF', 'CNY', 'HKD', 'SGD', 'KRW', 'INR', 'RUB', 'TRY', 'UAH', 'VND', 'THB', 'PHP', 'IDR', 'MYR', 'AED', 'SAR', 'ZAR', 'MXN', 'ARS', 'CLP', 'COP', 'PEN'}

# --- Comandos do Bot ---
@bot.message_handler(commands=['start', 'ajuda'])
def send_welcome(message):
    bot.reply_to(message, "Bot v11.2 (Comando Saldo) online. Use /status para ver as configurações.")

# =================================================================
# COMANDO /saldo ADICIONADO (v11.2)
# =================================================================
@bot.message_handler(commands=['saldo'])
def send_balance_command(message):
    try:
        bot.reply_to(message, "Buscando saldo na OKX...")
        balance = exchange.fetch_balance()
        saldo_usdt = balance.get('USDT', {'free': 0, 'total': 0})
        saldo_livre = Decimal(str(saldo_usdt.get('free', '0')))
        saldo_total = Decimal(str(saldo_usdt.get('total', '0')))
        
        reply = (f"📊 **Saldo em USDT (OKX):**\n"
                 f"Disponível para Trade: `{saldo_livre:.4f}`\n"
                 f"Total (incl. em ordens): `{saldo_total:.4f}`")
        bot.send_message(message.chat.id, reply, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ Erro ao buscar saldo: {e}")
        logging.error(f"Erro no comando /saldo: {e}")
# =================================================================

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
        command, value = message.text.split(maxsplit=1)
        command = command.split('@')[0][1:]
        
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
        bot.reply_to(message, f"Erro no comando. Uso: /{message.text.split()[0][1:]} <valor>")
        logging.error(f"Erro ao processar comando '{message.text}': {e}")

# --- Lógica de Arbitragem (sem alterações) ---
class ArbitrageEngine:
    def __init__(self, exchange_instance):
        self.exchange = exchange_instance
        self.markets = self.exchange.markets
        self.graph = {}
        self.rotas_viaveis = []
        self.last_depth = state['max_depth']

    def construir_rotas(self):
        logging.info("Construindo mapa de rotas...")
        self.graph = {}
        active_markets = {s: m for s, m in self.markets.items() if m.get('active') and m.get('base') and m.get('quote') and m['base'] not in FIAT_CURRENCIES and m['quote'] not in FIAT_CURRENCIES}
        for symbol, market in active_markets.items():
            base, quote = market['base'], market['quote']
            if base not in self.graph: self.graph[base] = []
            if quote not in self.graph: self.graph[quote] = []
            self.graph[base].append(quote)
            self.graph[quote].append(base)
        
        todas_as_rotas = []
        def encontrar_ciclos_dfs(u, path, depth):
            if depth > state['max_depth']: return
            for v in self.graph.get(u, []):
                if v == MOEDA_BASE_OPERACIONAL and len(path) >= MIN_ROUTE_DEPTH:
                    rota = path + [v]
                    if len(set(rota)) == len(rota) - 1: todas_as_rotas.append(rota)
                elif v not in path: encontrar_ciclos_dfs(v, path + [v], depth + 1)
        
        encontrar_ciclos_dfs(MOEDA_BASE_OPERACIONAL, [MOEDA_BASE_OPERACIONAL], 1)
        self.rotas_viaveis = [tuple(rota) for rota in todas_as_rotas]
        random.shuffle(self.rotas_viaveis)
        self.last_depth = state['max_depth']
        logging.info(f"Mapa de rotas reconstruído para profundidade {self.last_depth}. {len(self.rotas_viaveis)} rotas encontradas.")
        bot.send_message(CHAT_ID, f"🗺️ Mapa de rotas reconstruído para profundidade {self.last_depth}. {len(self.rotas_viaveis)} rotas encontradas.")

    def _get_pair_details(self, coin_from, coin_to):
        pair_buy = f"{coin_to}/{coin_from}"
        if pair_buy in self.markets: return pair_buy, 'buy'
        pair_sell = f"{coin_from}/{coin_to}"
        if pair_sell in self.markets: return pair_sell, 'sell'
        return None, None

    def _simular_trade(self, cycle_path, volume_inicial):
        current_amount = volume_inicial
        for i in range(len(cycle_path) - 1):
            coin_from, coin_to = cycle_path[i], cycle_path[i+1]
            pair_id, side = self._get_pair_details(coin_from, coin_to)
            if not pair_id: return None
            
            orderbook = self.exchange.fetch_order_book(pair_id)
            orders = orderbook['asks'] if side == 'buy' else orderbook['bids']
            if not orders: return None
            
            remaining_amount = current_amount
            final_traded_amount = Decimal('0')
            for price, size, *_ in orders:
                price, size = Decimal(str(price)), Decimal(str(size))
                if side == 'buy':
                    cost_for_step = remaining_amount
                    if cost_for_step <= price * size:
                        final_traded_amount += cost_for_step / price
                        remaining_amount = Decimal('0'); break
                    else:
                        final_traded_amount += size
                        remaining_amount -= price * size
                else: # side == 'sell'
                    if remaining_amount <= size:
                        final_traded_amount += remaining_amount * price
                        remaining_amount = Decimal('0'); break
                    else:
                        final_traded_amount += size * price
                        remaining_amount -= size
            if remaining_amount > 0: return None
            current_amount = final_traded_amount * (Decimal(1) - TAXA_TAKER)
        
        lucro_percentual = ((current_amount - volume_inicial) / volume_inicial) * 100
        return {'cycle': cycle_path, 'profit': lucro_percentual, 'final_amount': current_amount}

    def _executar_trade(self, cycle_path, volume_a_usar):
        try:
            bot.send_message(CHAT_ID, f"🚀 **MODO REAL** 🚀\nIniciando execução da rota: `{' -> '.join(cycle_path)}`\nVolume: `{volume_a_usar:.2f} USDT`", parse_mode="Markdown")
            current_amount_asset = volume_a_usar
            
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                
                market = self.exchange.market(pair_id)
                amount_to_trade = self.exchange.amount_to_precision(pair_id, current_amount_asset)
                
                logging.info(f"Perna {i+1}: {side.upper()} {amount_to_trade} {market['base'] if side == 'sell' else market['quote']} no par {pair_id}")
                
                if side == 'buy':
                    order = self.exchange.create_market_buy_order(pair_id, current_amount_asset)
                else: # side == 'sell'
                    order = self.exchange.create_market_sell_order(pair_id, amount_to_trade)
                
                time.sleep(1.5)
                order_status = self.exchange.fetch_order(order['id'], pair_id)

                if order_status['status'] != 'closed':
                    raise Exception(f"Ordem {order['id']} não foi completamente preenchida a tempo. Status: {order_status['status']}")

                filled_amount = Decimal(str(order_status['filled']))
                if side == 'buy':
                    current_amount_asset = filled_amount * (Decimal(1) - TAXA_TAKER)
                else: # side == 'sell'
                    filled_price = Decimal(str(order_status['average']))
                    current_amount_asset = (filled_amount * filled_price) * (Decimal(1) - TAXA_TAKER)
            
            lucro_real_usdt = current_amount_asset - volume_a_usar
            lucro_real_percent = (lucro_real_usdt / volume_a_usar) * 100
            bot.send_message(CHAT_ID, f"✅ **SUCESSO!**\nRota Concluída: `{' -> '.join(cycle_path)}`\nLucro: `{lucro_real_usdt:.4f} USDT` (`{lucro_real_percent:.4f}%`)", parse_mode="Markdown")

        except Exception as e:
            logging.critical(f"FALHA NA EXECUÇÃO DO TRADE: {e}")
            bot.send_message(CHAT_ID, f"🔴 **FALHA CRÍTICA NA EXECUÇÃO** 🔴\nRota: `{' -> '.join(cycle_path)}`\nErro: `{e}`\n**VERIFIQUE A CONTA MANUALMENTE!**", parse_mode="Markdown")

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

                ciclo_num += 1
                logging.info(f"--- Iniciando Ciclo #{ciclo_num} | Modo: {'Simulação' if state['dry_run'] else '⚠️ REAL ⚠️'} | Lucro Mín: {state['min_profit']}% ---")
                
                balance = self.exchange.fetch_balance()
                saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
                volume_a_usar = (saldo_disponivel * (state['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA

                if volume_a_usar < MINIMO_ABSOLUTO_USDT:
                    logging.warning(f"Volume ({volume_a_usar:.2f} USDT) abaixo do mínimo. Aguardando 30s.")
                    time.sleep(30)
                    continue

                melhor_oportunidade = None
                for i, cycle_tuple in enumerate(self.rotas_viaveis):
                    if not state['is_running']: break
                    
                    if i > 0 and i % 200 == 0: logging.info(f"Analisando rota {i}/{len(self.rotas_viaveis)}...")

                    resultado = self._simular_trade(list(cycle_tuple), volume_a_usar)
                    if resultado and resultado['profit'] > state['min_profit']:
                        if not melhor_oportunidade or resultado['profit'] > melhor_oportunidade['profit']:
                            melhor_oportunidade = resultado
                            msg = f"✅ Nova oportunidade encontrada: Lucro de `{melhor_oportunidade['profit']:.4f}%`\nRota: `{' -> '.join(melhor_oportunidade['cycle'])}`"
                            logging.info(msg)
                            bot.send_message(CHAT_ID, msg, parse_mode="Markdown")
                
                if melhor_oportunidade:
                    if not state['dry_run']:
                        self._executar_trade(melhor_oportunidade['cycle'], volume_a_usar)
                    else:
                        logging.info(f"MODO SIMULAÇÃO: Oportunidade de {melhor_oportunidade['profit']:.4f}% encontrada, mas não executada.")
                
                logging.info(f"Ciclo #{ciclo_num} concluído. Aguardando 20 segundos.")
                time.sleep(20)

            except Exception as e:
                logging.critical(f"Erro CRÍTICO no ciclo de análise: {e}")
                bot.send_message(CHAT_ID, f"🔴 **Erro Crítico no Motor** 🔴\n`{e}`\nO bot tentará novamente em 60 segundos.")
                time.sleep(60)

# --- Iniciar Tudo ---
if __name__ == "__main__":
    logging.info("Iniciando o bot v11.2 (Comando Saldo)...")
    
    engine = ArbitrageEngine(exchange)
    
    engine_thread = threading.Thread(target=engine.main_loop)
    engine_thread.daemon = True
    engine_thread.start()
    
    logging.info("Motor rodando em uma thread. Iniciando polling do Telebot...")
    bot.send_message(CHAT_ID, "✅ **Bot Gênesis v11.2 (Comando Saldo) iniciado com sucesso!**")
    bot.polling(non_stop=True)
