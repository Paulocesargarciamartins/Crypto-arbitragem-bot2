# bot.py - v15.3 - Sniper de Arbitragem OKX via Telegram

import os
import logging
import telebot
import ccxt
import time
from decimal import Decimal, getcontext, ROUND_DOWN
import threading
import random

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
    if 'bot' in locals() and CHAT_ID:
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
    bot.reply_to(message, "Bot v15.3 (Sniper de Arbitragem) online. Use /status.")

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

# --- Engine de Arbitragem ---
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

        active_markets = {s: m for s, m in self.markets.items() 
                          if m.get('active') and m.get('base') and m.get('quote') 
                          and m['base'] not in FIAT_CURRENCIES and m['quote'] not in FIAT_CURRENCIES
                          and m['base'] not in BLACKLIST_MOEDAS and m['quote'] not in BLACKLIST_MOEDAS}

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

    # --- Loop principal ---
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
                        bot.send_message(CHAT_ID, f"🚨 **STOP-LOSS ATINGIDO!** 🚨\nSaldo atual: `{saldo_total_usdt:.2f} USDT`\nO motor foi pausado automaticamente.", parse_mode="Markdown")
                        state['stop_loss_usdt'] = None
                        continue

                ciclo_num += 1
                volumes_a_usar = {}
                for moeda in MOEDAS_BASE_OPERACIONAIS:
                    saldo_disponivel = Decimal(str(balance.get('free', {}).get(moeda, '0')))
                    volumes_a_usar[moeda] = (saldo_disponivel * (state['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA

                with self.lock:
                    self._fetch_all_order_books()
                if not self.order_books
