# bot.py - v13.2 - O Sniper de Arbitragem (Estratégia de Snapshot)

import os
import logging
import telebot
import ccxt
import time
from decimal import Decimal, getcontext
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

# --- Estado do Bot ---
state = {
    'is_running': True,
    'dry_run': True,
    'min_profit': Decimal("0.4"),
    'volume_percent': Decimal("100.0"),
    'max_depth': 3,
    'stop_loss_usdt': None  # Valor em USDT para pausar o bot
}

# --- Parâmetros de Trade ---
TAXA_TAKER = Decimal("0.001")
MOEDA_BASE_OPERACIONAL = 'USDT'
MINIMO_ABSOLUTO_USDT = Decimal("3.1")
MIN_ROUTE_DEPTH = 3
MARGEM_DE_SEGURANCA = Decimal("0.997")  # ALTERADO: de 0.995 para 0.997
FIAT_CURRENCIES = {'USD', 'EUR', 'GBP', 'JPY', 'BRL', 'AUD', 'CAD', 'CHF', 'CNY', 'HKD', 'SGD', 'KRW', 'INR', 'RUB', 'TRY', 'UAH', 'VND', 'THB', 'PHP', 'IDR', 'MYR', 'AED', 'SAR', 'ZAR', 'MXN', 'ARS', 'CLP', 'COP', 'PEN'}
BLACKLIST_MOEDAS = {'TON'}  # ALTERADO: USDC removido


# --- Comandos do Bot ---
@bot.message_handler(commands=['start', 'ajuda'])
def send_welcome(message):
    bot.reply_to(message, "Bot v13.2 (Sniper de Arbitragem) online. Use /status.")

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
        saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
        volume_a_usar = (saldo_disponivel * (state['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA
        
        melhores, piores = engine._simular_todas_as_rotas(volume_a_usar)

        msg_melhores = "📊 **Radar de Depuração (Melhores Rotas Simuladas)**\n\n"
        for i, res in enumerate(melhores, 1):
            arrow = "✅" if res['profit'] >= 0 else "🔽"
            msg_melhores += f"{i}. Rota: `{' -> '.join(res['cycle'])}`\n   Lucro Líquido Realista: `{arrow} {res['profit']:.4f}%`\n"

        msg_piores = "\n\n📉 **Radar de Depuração (Piores Rotas Simuladas)**\n\n"
        for i, res in enumerate(piores, 1):
            arrow = "✅" if res['profit'] >= 0 else "🔽"
            msg_piores += f"{i}. Rota: `{' -> '.join(res['cycle'])}`\n   Lucro Líquido Realista: `{arrow} {res['profit']:.4f}%`\n"

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
        self.tickers = {}

    def construir_rotas(self):
        logging.info("Construindo mapa de rotas...")
        self.graph = {}
        active_markets = {
            s: m for s, m in self.markets.items() 
            if m.get('active') 
            and m.get('base') and m.get('quote') 
            and m['base'] not in FIAT_CURRENCIES and m['quote'] not in FIAT_CURRENCIES
            and m['base'] not in BLACKLIST_MOEDAS and m['quote'] not in BLACKLIST_MOEDAS
        }
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
        current_coin = MOEDA_BASE_OPERACIONAL

        for i in range(len(cycle_path) - 1):
            coin_from, coin_to = cycle_path[i], cycle_path[i+1]
            
            if coin_from != current_coin: return None

            pair_id, side = self._get_pair_details(coin_from, coin_to)
            if not pair_id: return None
            
            ticker = self.tickers.get(pair_id)
            if not ticker: return None

            price = ticker.get('ask') if side == 'buy' else ticker.get('bid')
            if not price: return None
            price = Decimal(str(price))

            volume_obtido = current_amount / price if side == 'buy' else current_amount * price
            current_amount = volume_obtido * (Decimal(1) - TAXA_TAKER)
            current_coin = coin_to
        
        if current_coin != MOEDA_BASE_OPERACIONAL: return None

        lucro_percentual = ((current_amount - volume_inicial) / volume_inicial) * 100
        return {'cycle': cycle_path, 'profit': lucro_percentual}

    def _simular_todas_as_rotas(self, volume_inicial):
        """
        Simula todas as rotas e retorna as 10 melhores e 10 piores.
        """
        if not self.tickers:
            self.tickers = self.exchange.fetch_tickers()

        resultados = []
        for cycle_tuple in self.rotas_viaveis:
            resultado = self._simular_trade(list(cycle_tuple), volume_inicial)
            if resultado:
                resultados.append(resultado)

        resultados.sort(key=lambda x: x['profit'], reverse=True)

        melhores = resultados[:10]
        piores = resultados[-10:]

        return melhores, piores

    def _executar_trade(self, cycle_path, volume_a_usar):
        bot.send_message(CHAT_ID, f"🚀 **MODO REAL** 🚀\nIniciando execução da rota: `{' -> '.join(cycle_path)}`\nVolume: `{volume_a_usar:.2f} USDT`", parse_mode="Markdown")
        
        moedas_presas = []
        current_amount = volume_a_usar
        current_asset = MOEDA_BASE_OPERACIONAL

        for i in range(len(cycle_path) - 1):
            coin_from, coin_to = cycle_path[i], cycle_path[i+1]
            
            try:
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                if not pair_id: raise Exception(f"Par inválido {coin_from}/{coin_to}")

                if side == 'buy':
                    trade_volume = self.exchange.cost_to_precision(pair_id, current_amount)
                    logging.info(f"Perna {i+1}: Comprando {coin_to} com {trade_volume:.4f} {current_asset} no par {pair_id}")
                    order = self.exchange.create_market_buy_order(pair_id, trade_volume)
                    
                else: # side == 'sell'
                    trade_volume = self.exchange.amount_to_precision(pair_id, current_amount)
                    logging.info(f"Perna {i+1}: Vendendo {trade_volume} {current_asset} para {coin_to} no par {pair_id}")
                    order = self.exchange.create_market_sell_order(pair_id, trade_volume)
                
                time.sleep(1.5)
                order_status = self.exchange.fetch_order(order['id'], pair_id)

                if order_status['status'] != 'closed':
                    raise Exception(f"Ordem {order['id']} não foi completamente preenchida. Status: {order_status['status']}")

                filled_amount = Decimal(str(order_status['filled']))
                filled_cost = Decimal(str(order_status['cost']))
                
                if side == 'buy':
                    current_amount = filled_amount * (Decimal(1) - TAXA_TAKER)
                    current_asset = coin_to
                else: # side == 'sell'
                    current_amount = filled_cost * (Decimal(1) - TAXA_TAKER)
                    current_asset = coin_to

                moedas_presas.append({'symbol': current_asset, 'amount': current_amount})
            
            except Exception as leg_error:
                logging.critical(f"FALHA NA PERNA {i+1} ({coin_from}->{coin_to}): {leg_error}")
                bot.send_message(CHAT_ID, f"🔴 **FALHA NA PERNA {i+1} da Rota!**\n`{' -> '.join(cycle_path)}`\n**Erro:** `{leg_error}`", parse_mode="Markdown")
                
                if moedas_presas:
                    ativo_preso_details = moedas_presas[-1]
                    ativo_symbol, ativo_amount = ativo_preso_details['symbol'], ativo_preso_details['amount']
                    
                    bot.send_message(CHAT_ID, f"⚠️ **CAPITAL PRESO!**\nAtivo: `{ativo_amount:.8f} {ativo_symbol}`.\n**Iniciando venda de emergência para USDT...**", parse_mode="Markdown")
                    
                    try:
                        reversal_pair, reversal_side = self._get_pair_details(ativo_symbol, MOEDA_BASE_OPERACIONAL)
                        if not reversal_pair:
                            raise Exception(f"Par de reversão {ativo_symbol}/{MOEDA_BASE_OPERACIONAL} não encontrado.")

                        if reversal_side == 'buy':
                            reversal_volume = self.exchange.cost_to_precision(reversal_pair, ativo_amount)
                            self.exchange.create_market_buy_order(reversal_pair, reversal_volume)
                        else:
                            reversal_volume = self.exchange.amount_to_precision(reversal_pair, ativo_amount)
                            self.exchange.create_market_sell_order(reversal_pair, reversal_volume)
                            
                        bot.send_message(CHAT_ID, "✅ **Venda de Emergência EXECUTADA!**", parse_mode="Markdown")

                    except Exception as reversal_error:
                        bot.send_message(CHAT_ID, f"❌ **FALHA CRÍTICA NA VENDA DE EMERGÊNCIA:** `{reversal_error}`. **VERIFIQUE A CONTA MANUALMENTE!**", parse_mode="Markdown")
                return
        
        lucro_real_usdt = current_amount - volume_a_usar
        lucro_real_percent = (lucro_real_usdt / volume_a_usar) * 100
        bot.send_message(CHAT_ID, f"✅ **SUCESSO!**\nRota Concluída: `{' -> '.join(cycle_path)}`\nLucro: `{lucro_real_usdt:.4f} USDT` (`{lucro_real_percent:.4f}%`)")


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
                    saldo_total_usdt = Decimal(str(balance.get('total', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
                    if saldo_total_usdt < state['stop_loss_usdt']:
                        state['is_running'] = False
                        logging.warning(f"STOP-LOSS ATINGIDO! Saldo {saldo_total_usdt:.2f} USDT < {state['stop_loss_usdt']:.2f} USDT. Operações pausadas.")
                        bot.send_message(CHAT_ID, f"🚨 **STOP-LOSS ATINGIDO!** 🚨\nSaldo atual: `{saldo_total_usdt:.2f} USDT`\nLimite: `{state['stop_loss_usdt']:.2f} USDT`\n**O motor foi pausado automaticamente.**", parse_mode="Markdown")
                        state['stop_loss_usdt'] = None
                        continue

                ciclo_num += 1
                logging.info(f"--- Iniciando Ciclo #{ciclo_num} | Modo: {'Simulação' if state['dry_run'] else '⚠️ REAL ⚠️'} | Lucro Mín: {state['min_profit']}% ---")
                
                saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
                volume_a_usar = (saldo_disponivel * (state['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA

                if volume_a_usar < MINIMO_ABSOLUTO_USDT:
                    logging.warning(f"Volume ({volume_a_usar:.2f} USDT) abaixo do mínimo. Aguardando 30s.")
                    time.sleep(30)
                    continue
                
                self.tickers = self.exchange.fetch_tickers()

                for i, cycle_tuple in enumerate(self.rotas_viaveis):
                    if not state['is_running']: break
                    if i > 0 and i % 250 == 0: logging.info(f"Analisando rota {i}/{len(self.rotas_viaveis)}...")

                    resultado = self._simular_trade(list(cycle_tuple), volume_a_usar)
                    
                    if resultado and resultado['profit'] > state['min_profit']:
                        msg = f"✅ **OPORTUNIDADE**\nLucro: `{resultado['profit']:.4f}%`\nRota: `{' -> '.join(resultado['cycle'])}`"
                        logging.info(msg)
                        bot.send_message(CHAT_ID, msg, parse_mode="Markdown")
                        
                        if not state['dry_run']:
                            self._executar_trade(resultado['cycle'], volume_a_usar)
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
    logging.info("Iniciando o bot v13.2 (Sniper de Arbitragem)...")
    
    engine = ArbitrageEngine(exchange)
    
    engine_thread = threading.Thread(target=engine.main_loop)
    engine_thread.daemon = True
    engine_thread.start()
    
    logging.info("Motor rodando em uma thread. Iniciando polling do Telebot...")
    try:
        bot.send_message(CHAT_ID, "✅ **Bot Gênesis v13.2 (Sniper de Arbitragem) iniciado com sucesso!**")
        bot.polling(non_stop=True)
    except Exception as e:
        logging.critical(f"Não foi possível iniciar o polling do Telegram ou enviar mensagem inicial: {e}")
