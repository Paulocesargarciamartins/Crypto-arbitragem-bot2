# bot.py - v14.0 - O Sniper de Arbitragem (The Robust One)

import os
import logging
import telebot
import ccxt
import time
from decimal import Decimal, getcontext, ConversionSyntax
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

# --- Internacionalização (i18n) ---
LANG = {
    'pt': {
        'welcome': "Bot v14.0 (Sniper de Arbitragem) online. Use /status.",
        'lang_set': "Idioma alterado para Português.",
        'lang_usage': "Uso: /lang <pt|en>",
        'fetching_balance': "Buscando saldos na OKX...",
        'balance_title': "📊 **Saldos (OKX):**\n",
        'balance_free': "Disponível para Trade",
        'balance_total': "Total (incl. em ordens)",
        'error_balance': "❌ Erro ao buscar saldos: {e}",
        'status_running': "Em operação",
        'status_paused': "Pausado",
        'mode_real': "⚠️ MODO REAL ⚠️",
        'mode_simulation': "Simulação",
        'status_title': "Status",
        'status_mode': "Modo",
        'status_min_profit': "Lucro Mínimo",
        'status_volume': "Volume por Trade",
        'status_depth': "Profundidade Máx. de Rotas",
        'status_stop_loss': "Stop Loss",
        'status_not_set': "Não definido",
        'engine_paused': "Motor de arbitragem pausado.",
        'engine_resumed': "Motor de arbitragem retomado.",
        'real_mode_activated': "⚠️ MODO REAL ATIVADO! ⚠️ As próximas oportunidades serão executadas.",
        'sim_mode_activated': "Modo Simulação ativado.",
        'min_profit_set': "Lucro mínimo definido para {val:.4f}%",
        'volume_set': "Volume de trade definido para {val:.2f}%",
        'volume_error': "Volume deve ser entre 1 e 100.",
        'depth_set': "Profundidade de rotas definida para {val}. O mapa será reconstruído no próximo ciclo.",
        'depth_error': "Profundidade deve ser entre {min} e 5.",
        'stoploss_off': "Stop loss desativado.",
        'stoploss_set': "Stop loss definido para {val:.2f} USDT.",
        'command_error': "Erro no comando. Uso: /{cmd} <valor>",
        'opportunity_found': "✅ **OPORTUNIDADE**\nLucro: `{profit:.4f}%`\nRota: `{' -> '.join(cycle)}`",
        'sim_mode_notice': "MODO SIMULAÇÃO: Oportunidade não executada.",
        'real_mode_executing': "🚀 **MODO REAL** 🚀\nIniciando execução da rota: `{' -> '.join(cycle)}`\nVolume: `{volume:.2f} {base}`",
        'route_failed': "🔴 **FALHA NA ROTA!**\n{details}",
        'capital_stuck': "⚠️ **CAPITAL PRESO!**\nAtivo: `{asset}`.\n**Iniciando venda de emergência para {base}...**",
        'emergency_sell_ok': "✅ **Venda de Emergência EXECUTADA!** Capital resgatado para `{base}`.",
        'emergency_sell_failed': "❌ **FALHA CRÍTICA NA VENDA DE EMERGÊNCIA:** `{e}`. **VERIFIQUE A CONTA MANUALMENTE!**",
        'emergency_sell_not_needed': "ℹ️ Saldo do ativo preso ({asset}) é zero. Nenhuma venda de emergência necessária.",
        'route_success': "✅ **SUCESSO!**\nRota Concluída: `{' -> '.join(cycle)}`\nLucro: `{profit_val:.4f} {base}` (`{profit_pct:.4f}%)",
        'stoploss_hit': "🚨 **STOP-LOSS ATINGIDO!** 🚨\nSaldo atual: `{balance:.2f} USDT`\nLimite: `{limit:.2f} USDT`\n**O motor foi pausado automaticamente.**",
        'critical_error_engine': "🔴 **Erro Crítico no Motor** 🔴\n`{e}`\nO bot tentará novamente em 60 segundos.",
        'bot_started': "✅ **Bot Gênesis v14.0 (The Robust One) iniciado com sucesso!**",
        'init_failed': "ERRO CRÍTICO NA INICIALIZAÇÃO: {e}. O bot não pode iniciar.",
        'map_rebuilt': "🗺️ Mapa de rotas reconstruído para profundidade {depth}. {count} rotas encontradas.",
    },
    'en': {
        'welcome': "Bot v14.0 (Arbitrage Sniper) online. Use /status.",
        'lang_set': "Language changed to English.",
        'lang_usage': "Usage: /lang <pt|en>",
        'fetching_balance': "Fetching balances from OKX...",
        'balance_title': "📊 **Balances (OKX):**\n",
        'balance_free': "Available for Trade",
        'balance_total': "Total (incl. in orders)",
        'error_balance': "❌ Error fetching balances: {e}",
        'status_running': "Running",
        'status_paused': "Paused",
        'mode_real': "⚠️ LIVE MODE ⚠️",
        'mode_simulation': "Simulation",
        'status_title': "Status",
        'status_mode': "Mode",
        'status_min_profit': "Minimum Profit",
        'status_volume': "Volume per Trade",
        'status_depth': "Max Route Depth",
        'status_stop_loss': "Stop Loss",
        'status_not_set': "Not set",
        'engine_paused': "Arbitrage engine paused.",
        'engine_resumed': "Arbitrage engine resumed.",
        'real_mode_activated': "⚠️ LIVE MODE ACTIVATED! ⚠️ Next opportunities will be executed.",
        'sim_mode_activated': "Simulation Mode activated.",
        'min_profit_set': "Minimum profit set to {val:.4f}%",
        'volume_set': "Trade volume set to {val:.2f}%",
        'volume_error': "Volume must be between 1 and 100.",
        'depth_set': "Route depth set to {val}. The map will be rebuilt on the next cycle.",
        'depth_error': "Depth must be between {min} and 5.",
        'stoploss_off': "Stop loss disabled.",
        'stoploss_set': "Stop loss set to {val:.2f} USDT.",
        'command_error': "Error in command. Usage: /{cmd} <value>",
        'opportunity_found': "✅ **OPPORTUNITY**\nProfit: `{profit:.4f}%`\nRoute: `{' -> '.join(cycle)}`",
        'sim_mode_notice': "SIMULATION MODE: Opportunity not executed.",
        'real_mode_executing': "🚀 **LIVE MODE** 🚀\nExecuting route: `{' -> '.join(cycle)}`\nVolume: `{volume:.2f} {base}`",
        'route_failed': "🔴 **ROUTE FAILED!**\n{details}",
        'capital_stuck': "⚠️ **CAPITAL STUCK!**\nAsset: `{asset}`.\n**Initiating emergency sell to {base}...**",
        'emergency_sell_ok': "✅ **Emergency Sell EXECUTED!** Capital recovered to `{base}`.",
        'emergency_sell_failed': "❌ **CRITICAL FAILURE ON EMERGENCY SELL:** `{e}`. **CHECK ACCOUNT MANUALLY!**",
        'emergency_sell_not_needed': "ℹ️ Stuck asset balance ({asset}) is zero. No emergency sell needed.",
        'route_success': "✅ **SUCCESS!**\nRoute Completed: `{' -> '.join(cycle)}`\nProfit: `{profit_val:.4f} {base}` (`{profit_pct:.4f}%)",
        'stoploss_hit': "🚨 **STOP-LOSS HIT!** 🚨\nCurrent balance: `{balance:.2f} USDT`\nLimit: `{limit:.2f} USDT`\n**The engine has been paused automatically.**",
        'critical_error_engine': "🔴 **Critical Engine Error** 🔴\n`{e}`\nThe bot will try again in 60 seconds.",
        'bot_started': "✅ **Bot Genesis v14.0 (The Robust One) started successfully!**",
        'init_failed': "CRITICAL ERROR ON INITIALIZATION: {e}. The bot cannot start.",
        'map_rebuilt': "🗺️ Route map rebuilt for depth {depth}. {count} routes found.",
    }
}

# --- Estado do Bot ---
state = {
    'is_running': True,
    'dry_run': True,
    'min_profit': Decimal("0.4"),
    'volume_percent': Decimal("100.0"),
    'max_depth': 3,
    'stop_loss_usdt': None,
    'lang': 'pt'
}

def get_text(key, **kwargs):
    return LANG[state['lang']].get(key, key).format(**kwargs)

# --- Inicialização ---
try:
    if not TOKEN or not CHAT_ID:
        raise ValueError("As variáveis de ambiente TELEGRAM_TOKEN e TELEGRAM_CHAT_ID são obrigatórias.")
    bot = telebot.TeleBot(TOKEN)
    exchange = ccxt.okx({
        'apiKey': OKX_API_KEY,
        'secret': OKX_API_SECRET,
        'password': OKX_API_PASSWORD,
    })
    exchange.load_markets()
    logging.info("Bibliotecas Telebot e CCXT iniciadas com sucesso.")
except Exception as e:
    logging.critical(f"Falha ao iniciar bibliotecas: {e}")
    if bot and CHAT_ID:
        try:
            bot.send_message(CHAT_ID, get_text('init_failed', e=e))
        except Exception as alert_e:
            logging.error(f"Falha ao enviar alerta de erro de inicialização: {alert_e}")
    exit()

# --- Parâmetros de Trade ---
TAXA_TAKER = Decimal("0.001")
MOEDAS_BASE_OPERACIONAIS = ['USDT', 'USDC']
MINIMO_ABSOLUTO_DO_VOLUME = Decimal("3.1")
MIN_ROUTE_DEPTH = 3
MARGEM_DE_SEGURANCA = Decimal("0.997")
FIAT_CURRENCIES = {'USD', 'EUR', 'GBP', 'JPY', 'BRL', 'AUD', 'CAD', 'CHF', 'CNY', 'HKD', 'SGD', 'KRW', 'INR', 'RUB', 'TRY', 'UAH', 'VND', 'THB', 'PHP', 'IDR', 'MYR', 'AED', 'SAR', 'ZAR', 'MXN', 'ARS', 'CLP', 'COP', 'PEN'}
BLACKLIST_MOEDAS = {'TON', 'SUI', 'PI'}

# --- Comandos do Bot (sem alterações) ---
@bot.message_handler(commands=['start', 'ajuda'])
def send_welcome(message):
    bot.reply_to(message, get_text('welcome'))

@bot.message_handler(commands=['lang'])
def set_language(message):
    try:
        lang_code = message.text.split(maxsplit=1)[1].lower()
        if lang_code in LANG:
            state['lang'] = lang_code
            bot.reply_to(message, get_text('lang_set'))
        else:
            bot.reply_to(message, get_text('lang_usage'))
    except IndexError:
        bot.reply_to(message, get_text('lang_usage'))

@bot.message_handler(commands=['saldo'])
def send_balance_command(message):
    try:
        bot.reply_to(message, get_text('fetching_balance'))
        balance = exchange.fetch_balance()
        reply = get_text('balance_title')
        for moeda in MOEDAS_BASE_OPERACIONAIS:
            saldo = balance.get(moeda, {'free': 0, 'total': 0})
            saldo_livre = Decimal(str(saldo.get('free', '0')))
            saldo_total = Decimal(str(saldo.get('total', '0')))
            reply += (f"- `{moeda}`\n"
                      f"  {get_text('balance_free')}: `{saldo_livre:.4f}`\n"
                      f"  {get_text('balance_total')}: `{saldo_total:.4f}`\n")
        
        bot.send_message(message.chat.id, reply, parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, get_text('error_balance', e=e))
        logging.error(f"Erro no comando /saldo: {e}")

@bot.message_handler(commands=['status'])
def send_status(message):
    status_text = get_text('status_running') if state['is_running'] else get_text('status_paused')
    mode_text = get_text('mode_simulation') if state['dry_run'] else get_text('mode_real')
    stop_loss_text = f"{state['stop_loss_usdt']:.2f} USDT" if state['stop_loss_usdt'] else get_text('status_not_set')
    reply = (f"{get_text('status_title')}: {status_text}\n"
             f"{get_text('status_mode')}: **{mode_text}**\n"
             f"{get_text('status_min_profit')}: `{state['min_profit']:.4f}%`\n"
             f"{get_text('status_volume')}: `{state['volume_percent']:.2f}%`\n"
             f"{get_text('status_depth')}: `{state['max_depth']}`\n"
             f"{get_text('status_stop_loss')}: `{stop_loss_text}`")
    bot.send_message(message.chat.id, reply, parse_mode="Markdown")

@bot.message_handler(commands=['pausar', 'retomar', 'modo_real', 'modo_simulacao'])
def simple_commands(message):
    command = message.text.split('@')[0][1:]
    reply_key = {
        'pausar': 'engine_paused',
        'retomar': 'engine_resumed',
        'modo_real': 'real_mode_activated',
        'modo_simulacao': 'sim_mode_activated'
    }.get(command)

    if command == 'pausar': state['is_running'] = False
    elif command == 'retomar': state['is_running'] = True
    elif command == 'modo_real': state['dry_run'] = False
    elif command == 'modo_simulacao': state['dry_run'] = True
    
    if reply_key:
        bot.reply_to(message, get_text(reply_key))
        logging.info(f"Comando '{command}' executado por {message.from_user.username}.")

@bot.message_handler(commands=['setlucro', 'setvolume', 'setdepth', 'setstoploss'])
def value_commands(message):
    try:
        parts = message.text.split(maxsplit=1)
        command = parts[0].split('@')[0][1:]
        value = parts[1] if len(parts) > 1 else ""

        if command == 'setlucro':
            val = Decimal(value)
            state['min_profit'] = val
            bot.reply_to(message, get_text('min_profit_set', val=val))
        elif command == 'setvolume':
            val = Decimal(value)
            if 0 < val <= 100:
                state['volume_percent'] = val
                bot.reply_to(message, get_text('volume_set', val=val))
            else:
                bot.reply_to(message, get_text('volume_error'))
        elif command == 'setdepth':
            val = int(value)
            if MIN_ROUTE_DEPTH <= val <= 5:
                state['max_depth'] = val
                bot.reply_to(message, get_text('depth_set', val=val))
            else:
                bot.reply_to(message, get_text('depth_error', min=MIN_ROUTE_DEPTH))
        elif command == 'setstoploss':
            if value.lower() == 'off':
                state['stop_loss_usdt'] = None
                bot.reply_to(message, get_text('stoploss_off'))
            else:
                val = Decimal(value)
                state['stop_loss_usdt'] = val
                bot.reply_to(message, get_text('stoploss_set', val=val))
        
        logging.info(f"Comando '{command} {value}' executado por {message.from_user.username}.")
    except Exception as e:
        bot.reply_to(message, get_text('command_error', cmd=command))
        logging.error(f"Erro ao processar comando '{message.text}': {e}")

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
                if v == path[0] and len(path) >= MIN_ROUTE_DEPTH:
                    rota_completa = path + [v]
                    todas_as_rotas.append(rota_completa)
                elif v not in path:
                    encontrar_ciclos_dfs(v, path + [v], depth + 1)
        
        for base_moeda in MOEDAS_BASE_OPERACIONAIS:
            encontrar_ciclos_dfs(base_moeda, [base_moeda], 1)

        self.rotas_viaveis = [tuple(rota) for rota in todas_as_rotas]
        random.shuffle(self.rotas_viaveis)
        self.last_depth = state['max_depth']
        logging.info(f"Mapa de rotas reconstruído para profundidade {self.last_depth}. {len(self.rotas_viaveis)} rotas encontradas.")
        bot.send_message(CHAT_ID, get_text('map_rebuilt', depth=self.last_depth, count=len(self.rotas_viaveis)))

    def _get_pair_details(self, coin_from, coin_to):
        pair_buy = f"{coin_to}/{coin_from}"
        if pair_buy in self.markets: return pair_buy, 'buy'
        pair_sell = f"{coin_from}/{coin_to}"
        if pair_sell in self.markets: return pair_sell, 'sell'
        return None, None

    def _validar_perna_de_trade(self, pair_id, side, amount):
        """
        NOVA FUNÇÃO: Valida se uma perna de trade é executável em termos de limites.
        Retorna True se for válida, False caso contrário.
        """
        market_info = self.markets.get(pair_id)
        if not market_info: return False

        if side == 'buy':
            # 'amount' é o custo que queremos gastar (ex: em USDT)
            min_cost_str = market_info.get("limits", {}).get("cost", {}).get("min")
            min_cost = Decimal(str(min_cost_str)) if min_cost_str is not None else Decimal('0')
            if amount < min_cost:
                logging.debug(f"Validação falhou para {pair_id}: Custo {amount} < Mínimo {min_cost}")
                return False
        else: # side == 'sell'
            # 'amount' é a quantidade que queremos vender (ex: em PEPE)
            min_amount_str = market_info.get("limits", {}).get("amount", {}).get("min")
            min_amount = Decimal(str(min_amount_str)) if min_amount_str is not None else Decimal('0')
            if amount < min_amount:
                logging.debug(f"Validação falhou para {pair_id}: Quantidade {amount} < Mínima {min_amount}")
                return False
        
        return True

    def _simular_trade(self, cycle_path, volumes_iniciais):
        base_moeda = cycle_path[0]
        if base_moeda not in volumes_iniciais: return None
        volume_inicial = volumes_iniciais[base_moeda]
        
        current_amount = volume_inicial
        current_coin = base_moeda

        for i in range(len(cycle_path) - 1):
            coin_from, coin_to = cycle_path[i], cycle_path[i+1]
            if coin_from != current_coin: return None

            pair_id, side = self._get_pair_details(coin_from, coin_to)
            if not pair_id: return None
            
            ticker = self.tickers.get(pair_id)
            if not ticker: return None

            raw_price = ticker.get('ask') if side == 'buy' else ticker.get('bid')
            if raw_price is None: return None
            
            try:
                price = Decimal(str(raw_price))
            except ConversionSyntax: return None

            if price == 0: return None

            # Validação de limites ANTES de prosseguir
            validation_amount = current_amount if side == 'buy' else (current_amount / price)
            if not self._validar_perna_de_trade(pair_id, side, current_amount):
                return None # Descarta a rota se os limites não forem atendidos

            volume_obtido = current_amount / price if side == 'buy' else current_amount * price
            current_amount = volume_obtido * (Decimal(1) - TAXA_TAKER)
            current_coin = coin_to
        
        if current_coin != base_moeda: return None

        lucro_percentual = ((current_amount - volume_inicial) / volume_inicial) * 100
        return {'cycle': cycle_path, 'profit': lucro_percentual}

    def _simular_todas_as_rotas(self, volumes_iniciais):
        if not self.tickers:
            self.tickers = self.exchange.fetch_tickers()

        resultados = []
        for cycle_tuple in self.rotas_viaveis:
            resultado = self._simular_trade(list(cycle_tuple), volumes_iniciais)
            if resultado:
                resultados.append(resultado)

        resultados.sort(key=lambda x: x['profit'], reverse=True)
        return resultados[:10], resultados[-10:]
    
    def _formatar_erro_telegram(self, leg_error, perna, rota):
        erro_str = str(leg_error)
        detalhes = f"Falha na Perna {perna} da Rota: `{' -> '.join(rota)}`\n"
        
        if isinstance(leg_error, ccxt.ExchangeError):
            try:
                start_index = erro_str.find('{')
                end_index = erro_str.rfind('}') + 1
                if start_index != -1 and end_index != -1:
                    erro_json_str = erro_str[start_index:end_index]
                    erro_json = eval(erro_json_str)
                    data = erro_json.get('data', [{}])[0]
                    s_code = data.get('sCode', 'N/A')
                    s_msg = data.get('sMsg', 'N/A')
                    detalhes += f"Código de Erro OKX: `{s_code}`\n"
                    detalhes += f"Mensagem de Erro: `{s_msg}`\n"
                else:
                    detalhes += f"Detalhes do Erro: `{erro_str}`"
            except Exception:
                detalhes += f"Detalhes do Erro: `{erro_str}`"
        else:
            detalhes += f"Detalhes do Erro: `{erro_str}`"
        return detalhes

    def _executar_trade(self, cycle_path, volume_a_usar):
        base_moeda = cycle_path[0]
        bot.send_message(CHAT_ID, get_text('real_mode_executing', cycle=cycle_path, volume=volume_a_usar, base=base_moeda), parse_mode="Markdown")
        
        moedas_presas = []
        current_amount = volume_a_usar
        current_asset = base_moeda

        for i in range(len(cycle_path) - 1):
            coin_from, coin_to = cycle_path[i], cycle_path[i+1]
            
            try:
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                if not pair_id: raise Exception(f"Par inválido {coin_from}/{coin_to}")

                # Validação final antes de executar
                if not self._validar_perna_de_trade(pair_id, side, current_amount):
                    raise Exception(f"Validação final de limites falhou para {pair_id}")

                if side == 'buy':
                    cost_to_spend = self.exchange.cost_to_precision(pair_id, current_amount)
                    logging.info(f"DEBUG: Tentando COMPRAR no par {pair_id} GASTANDO {cost_to_spend} {coin_from}")
                    order = self.exchange.create_market_buy_order_with_cost(pair_id, cost_to_spend)
                else: # side == 'sell'
                    amount_to_sell = self.exchange.amount_to_precision(pair_id, current_amount)
                    logging.info(f"DEBUG: Tentando VENDER {amount_to_sell} {coin_from} no par {pair_id}")
                    order = self.exchange.create_market_sell_order(pair_id, amount_to_sell)
                
                time.sleep(1.5)
                order_status = self.exchange.fetch_order(order["id"], pair_id)

                if order_status["status"] != "closed":
                    raise Exception(f"Ordem {order['id']} não foi completamente preenchida. Status: {order_status['status']}")

                filled_amount = Decimal(str(order_status["filled"]))
                filled_cost = Decimal(str(order_status["cost"]))
                
                current_amount = filled_amount if side == 'buy' else filled_cost
                current_amount *= (Decimal(1) - TAXA_TAKER)
                current_asset = coin_to
                moedas_presas.append({'symbol': current_asset, 'amount': current_amount})
            
            except Exception as leg_error:
                logging.critical(f"FALHA NA PERNA {i+1} ({coin_from}->{coin_to}): {leg_error}")
                mensagem_detalhada = self._formatar_erro_telegram(leg_error, i + 1, cycle_path)
                bot.send_message(CHAT_ID, get_text('route_failed', details=mensagem_detalhada), parse_mode="Markdown")
                
                if moedas_presas:
                    ativo_preso_details = moedas_presas[-1]
                    ativo_symbol = ativo_preso_details["symbol"]
                    
                    bot.send_message(CHAT_ID, get_text('capital_stuck', asset=ativo_symbol, base=base_moeda), parse_mode="Markdown")
                    
                    try:
                        time.sleep(1)
                        live_balance = self.exchange.fetch_balance()
                        ativo_amount = Decimal(str(live_balance.get(ativo_symbol, {}).get('free', '0')))
                        
                        if ativo_amount > 0:
                            reversal_pair, reversal_side = self._get_pair_details(ativo_symbol, base_moeda)
                            if not reversal_pair: raise Exception(f"Par de reversão {ativo_symbol}/{base_moeda} não encontrado.")

                            if reversal_side == 'buy':
                                self.exchange.create_market_buy_order_with_cost(reversal_pair, ativo_amount)
                            else:
                                amount_to_sell_reversal = self.exchange.amount_to_precision(reversal_pair, ativo_amount)
                                self.exchange.create_market_sell_order(reversal_pair, amount_to_sell_reversal)
                            
                            bot.send_message(CHAT_ID, get_text('emergency_sell_ok', base=base_moeda), parse_mode="Markdown")
                        else:
                            bot.send_message(CHAT_ID, get_text('emergency_sell_not_needed', asset=ativo_symbol), parse_mode="Markdown")
