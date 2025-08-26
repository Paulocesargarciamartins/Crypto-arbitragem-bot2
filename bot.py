import os
import asyncio
import logging
from decimal import Decimal, getcontext, ROUND_DOWN
import time
import sys
import gc
from collections import defaultdict, deque
import ccxt.async_support as ccxt
import telebot
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_helper import ApiTelegramException

# --- 1. CONFIGURAÇÕES GLOBAIS ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD")

# --- Pilares da Estratégia ---
TAXA_OPERACAO = Decimal("0.001")
MIN_PROFIT_DEFAULT = Decimal("0.001")
MARGEM_DE_SEGURANCA = Decimal("0.995")
MAX_ROUTE_DEPTH = 4
ORDER_BOOK_DEPTH = 100

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
getcontext().prec = 30

# --- Helpers ---
def _safe_decimal(value):
    try:
        if value is None or value == '':
            return None
        return Decimal(str(value))
    except Exception:
        return None

# --- 2. OKX API CLIENT ---
class OKXApiClient:
    def __init__(self, api_key, secret_key, password):
        self.exchange = ccxt.okx({
            'apiKey': api_key,
            'secret': secret_key,
            'password': password,
            'options': {'defaultType': 'spot'}
        })
        self.markets = None

    async def load_markets(self):
        try:
            self.markets = await self.exchange.load_markets()
            return self.markets
        except Exception as e:
            logger.error(f"Erro ao carregar mercados: {e}")
            return e

    async def _execute_api_call(self, api_call, *args, **kwargs):
        try:
            return await api_call(*args, **kwargs)
        except ccxt.ExchangeError as ex:
            logger.error(f"CCXT ExchangeError: {ex}")
            return ex
        except Exception as e:
            logger.error(f"Unknown API error: {e}")
            return None

    async def get_spot_balances(self):
        return await self._execute_api_call(self.exchange.fetch_balance)

    async def create_market_buy_order(self, symbol, amount_to_spend):
        return await self._execute_api_call(self.exchange.create_market_buy_order, symbol, amount_to_spend)
        
    async def create_market_sell_order(self, symbol, amount_to_sell):
        return await self._execute_api_call(self.exchange.create_market_sell_order, symbol, amount_to_sell)

    async def get_order_book(self, symbol):
        return await self._execute_api_call(self.exchange.fetch_order_book, symbol, limit=ORDER_BOOK_DEPTH)
    
    async def get_currency_pair(self, symbol):
        return self.markets.get(symbol)

    async def get_ticker(self, symbol):
        return await self._execute_api_call(self.exchange.fetch_ticker, symbol)

# --- 3. GÊNESIS ENGINE OTIMIZADO ---
class GenesisEngine:
    def __init__(self, bot_instance: AsyncTeleBot):
        self.bot = bot_instance
        self.bot_data = {}
        self.api_client = OKXApiClient(OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSWORD)
        self.bot_data.setdefault("is_running", True)
        self.bot_data.setdefault("min_profit", MIN_PROFIT_DEFAULT)
        self.bot_data.setdefault("dry_run", True)
        self.bot_data.setdefault("max_route_depth", MAX_ROUTE_DEPTH)
        self.pair_rules = {}
        self.all_currencies = []
        self.graph = defaultdict(list)
        self.all_cycles = []
        self.simulacao_data = []
        self.trade_lock = asyncio.Lock()
        self.stats = {
            "start_time": time.time(),
            "ciclos_verificacao_total": 0,
            "rotas_sobreviventes_total": 0,
            "ultimo_ciclo_timestamp": time.time()
        }
        self.stop_loss_monitoring_task = None
        self.routes_ready = asyncio.Event()
    
    async def build_routes_background(self):
        logger.info("Gênesis: Construção de rotas em segundo plano iniciada...")
        try:
            all_pairs_data = await self.api_client.load_markets()
            if not all_pairs_data or isinstance(all_pairs_data, ccxt.ExchangeError):
                logger.critical("Gênesis: Não foi possível obter os pares da OKX para construir rotas.")
                await self.bot.send_message(ADMIN_CHAT_ID, "❌ Falha crítica ao carregar mercados da OKX. A busca de rotas foi abortada.", parse_mode="Markdown")
                return

            self.pair_rules = {pair_id: pair_data for pair_id, pair_data in all_pairs_data.items() if pair_data.get('active')}
            
            self.graph = defaultdict(list)
            all_currencies_set = set()
            for pair_data in self.pair_rules.values():
                if pair_data.get('active'):
                    base, quote = pair_data['base'], pair_data['quote']
                    self.graph[base].append(quote)
                    self.graph[quote].append(base)
                    all_currencies_set.add(base)
                    all_currencies_set.add(quote)
            self.all_currencies = list(all_currencies_set)
            
            self.all_cycles = self._encontrar_ciclos_bfs()
            
            logger.info(f"Gênesis: Construção de rotas concluída. {len(self.all_cycles)} rotas encontradas.")
            await self.bot.send_message(ADMIN_CHAT_ID, f"✅ Motor de rotas construído! Encontradas {len(self.all_cycles)} rotas.", parse_mode="Markdown")
            
            self.routes_ready.set()

        except Exception as e:
            logger.error(f"Gênesis: Erro crítico na construção de rotas em segundo plano: {e}", exc_info=True)
            await self.bot.send_message(ADMIN_CHAT_ID, f"❌ Falha crítica ao construir rotas: `{e}`", parse_mode="Markdown")

    def _encontrar_ciclos_bfs(self):
        all_cycles = []
        for start_node in self.all_currencies:
            queue = deque([ (start_node, [start_node]) ])
            
            while queue:
                current_node, path = queue.popleft()
                
                if len(path) > self.bot_data["max_route_depth"]:
                    continue

                for neighbor in self.graph[current_node]:
                    if neighbor == start_node and len(path) >= 2:
                        cycle = path + [neighbor]
                        
                        # Verificação canônica para evitar duplicatas
                        canonical_cycle = tuple(sorted(cycle[:-1]))
                        if canonical_cycle not in {tuple(sorted(c[:-1])) for c in all_cycles}:
                             all_cycles.append(cycle)
                    elif neighbor not in path:
                        new_path = path + [neighbor]
                        queue.append((neighbor, new_path))
        return all_cycles
        
    def _get_pair_details(self, coin_from, coin_to):
        pair_v1 = f"{coin_from}/{coin_to}"
        if pair_v1 in self.pair_rules: return pair_v1, "sell"
        pair_v2 = f"{coin_to}/{coin_from}"
        if pair_v2 in self.pair_rules: return pair_v2, "buy"
        return None, None
        
    async def _simular_realidade(self, cycle_path, investimento_inicial, order_book_cache):
        try:
            valor_simulado = investimento_inicial
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                if not pair_id: return None
                
                pair_info = self.pair_rules.get(pair_id)
                if not pair_info: return None
                
                amount_prec = pair_info['precision']['amount'] if 'precision' in pair_info and 'amount' in pair_info['precision'] else 8
                quantizer = Decimal(f"1e-{amount_prec}")
                
                order_book = order_book_cache.get(pair_id)
                if not order_book: return None
                
                if side == "buy":
                    valor_a_gastar = valor_simulado
                    quantidade_comprada = Decimal("0")
                    for preco_str, quantidade_str in order_book['asks']:
                        preco, quantidade_disponivel = Decimal(str(preco_str)), Decimal(str(quantidade_str))
                        custo_nivel = preco * quantidade_disponivel
                        if valor_a_gastar > custo_nivel:
                            quantidade_comprada += quantidade_disponivel
                            valor_a_gastar -= custo_nivel
                        else:
                            if preco == 0: break
                            qtd_a_comprar = (valor_a_gastar / preco).quantize(quantizer, rounding=ROUND_DOWN)
                            if qtd_a_comprar <= 0: break
                            quantidade_comprada += qtd_a_comprar
                            valor_a_gastar = Decimal("0")
                            break
                    if valor_a_gastar > 0: return None
                    valor_simulado = quantidade_comprada
                else:
                    quantidade_a_vender = valor_simulado.quantize(quantizer, rounding=ROUND_DOWN)
                    if quantidade_a_vender <= 0: return None
                    valor_recebido = Decimal("0")
                    for preco_str, quantidade_str in order_book['bids']:
                        preco, quantidade_disponivel = Decimal(str(preco_str)), Decimal(str(quantidade_str))
                        if quantidade_a_vender > quantidade_disponivel:
                            valor_recebido += quantidade_disponivel * preco
                            quantidade_a_vender -= quantidade_disponivel
                        else:
                            valor_recebido += quantidade_a_vender * preco
                            quantidade_a_vender = Decimal("0")
                            break
                    if quantidade_a_vender > 0: return None
                    valor_simulado = valor_recebido
                valor_simulado *= (1 - TAXA_OPERACAO)

            if investimento_inicial == 0: return Decimal("0")
            return {"cycle": cycle_path, "profit": ((valor_simulado - investimento_inicial) / investimento_inicial) * 100}
        except Exception as e:
            logger.error(f"Erro na simulação para a rota {" -> ".join(cycle_path)}: {e}", exc_info=True)
            return None

    async def verificar_oportunidades(self):
        logger.info("Gênesis: Motor \"O Caçador de Migalhas\" (OKX) aguardando rotas...")
        await self.routes_ready.wait()
        logger.info("Gênesis: Rotas prontas! Iniciando busca por oportunidades.")

        while True:
            try:
                if not self.bot_data.get("is_running", True) or self.trade_lock.locked():
                    await asyncio.sleep(1)
                    continue
                
                self.stats["ciclos_verificacao_total"] += 1
                self.stats["ultimo_ciclo_timestamp"] = time.time()
                
                saldos = await self.api_client.get_spot_balances()
                if not saldos or isinstance(saldos, ccxt.ExchangeError):
                    await asyncio.sleep(5)
                    continue
                saldo_por_moeda = {c: Decimal(str(saldos.get(c, {}).get('free', '0'))) for c in saldos['free'] if Decimal(str(saldos.get(c, {}).get('free', '0'))) > 0}

                relevant_pairs = set()
                for cycle_path in self.all_cycles:
                    for i in range(len(cycle_path) - 1):
                        pair_id, _ = self._get_pair_details(cycle_path[i], cycle_path[i+1])
                        if pair_id:
                            relevant_pairs.add(pair_id)

                order_book_cache = {}
                tasks_ob = [self.api_client.get_order_book(pair) for pair in relevant_pairs]
                results_ob = await asyncio.gather(*tasks_ob, return_exceptions=True)
                for i, pair_id in enumerate(relevant_pairs):
                    if not isinstance(results_ob[i], Exception):
                        order_book_cache[pair_id] = results_ob[i]

                self.simulacao_data = []
                tasks = []
                
                for start_node, saldo in saldo_por_moeda.items():
                    if saldo <= 0: continue
                    
                    rotas_para_simular = [c for c in self.all_cycles if c[0] == start_node]
                    
                    for cycle_path in rotas_para_simular:
                        tasks.append(self._simular_realidade(cycle_path, saldo, order_book_cache))
                        
                results = await asyncio.gather(*tasks)
                self.simulacao_data = [res for res in results if res is not None]

                oportunidades_reais = sorted([op for op in self.simulacao_data if op["profit"] > self.bot_data["min_profit"]], key=lambda x: x["profit"], reverse=True)
                self.stats["rotas_sobreviventes_total"] += len(oportunidades_reais)

                if oportunidades_reais:
                    async with self.trade_lock:
                        melhor_oportunidade = oportunidades_reais[0]
                        logger.info(f"Gênesis: Oportunidade REALISTA encontrada ({melhor_oportunidade['profit']:.4f}%).")
                        await self._executar_trade_realista(melhor_oportunidade["cycle"])
            except Exception as e:
                logger.error(f"Gênesis: Erro no loop principal de verificação: {e}", exc_info=True)
            finally:
                await asyncio.sleep(10)

    async def _monitorar_stop_loss(self, moeda_destino, investimento_inicial, pair_to_monitor):
        logger.info(f"Monitoramento de stop-loss iniciado para {moeda_destino}.")
        try:
            await self.bot.send_message(ADMIN_CHAT_ID, f"⚠️ **Monitoramento de Stop-Loss Ativado!**\n"
                                        f"Ativo monitorado: `{moeda_destino}`\n"
                                        f"Investimento inicial: `{investimento_inicial}`", parse_mode="Markdown")
            
            saldo_inicial = investimento_inicial
            last_warning_level = None

            while True:
                saldos = await self.api_client.get_spot_balances()
                saldo_atual = Decimal(str(saldos.get(moeda_destino, {}).get('free', '0')))
                
                if saldo_atual <= 0:
                    break

                ticker = await self.api_client.get_ticker(pair_to_monitor)
                if not ticker or isinstance(ticker, ccxt.ExchangeError):
                    await asyncio.sleep(2)
                    continue

                preco_atual = Decimal(str(ticker['last']))
                valor_atual_em_moeda_base = saldo_atual * preco_atual
                
                perda_percentual = ((valor_atual_em_moeda_base - saldo_inicial) / saldo_inicial) * 100
                
                if perda_percentual < Decimal("-0.5"):
                    if last_warning_level != 1:
                        last_warning_level = 1
                        await self.bot.send_message(ADMIN_CHAT_ID, f"🚨 **ALERTA DE STOP-LOSS**\n"
                                                    f"Perda de `{perda_percentual:.2f}%`. Próximo nível de stop-loss em `{Decimal('-1.0')}%`.", parse_mode="Markdown")
                
                if perda_percentual < Decimal("-1.0"):
                    await self.bot.send_message(ADMIN_CHAT_ID, f"🛑 **STOP-LOSS CRÍTICO ATINGIDO!**\n"
                                                f"Perda de `{perda_percentual:.2f}%`. Vendendo todo o saldo de `{moeda_destino}`.", parse_mode="Markdown")
                    
                    await self.api_client.create_market_sell_order(pair_to_monitor, float(saldo_atual))
                    await self.bot.send_message(ADMIN_CHAT_ID, f"✅ **Venda de pânico concluída!**", parse_mode="Markdown")
                    break

                await asyncio.sleep(5)
                gc.collect()
        
        except Exception as e:
            logger.error(f"Erro no monitoramento de stop-loss: {e}", exc_info=True)
            await self.bot.send_message(ADMIN_CHAT_ID, f"❌ Erro no monitoramento de stop-loss: `{e}`", parse_mode="Markdown")
        finally:
            logger.info("Monitoramento de stop-loss finalizado.")

    async def _executar_trade_realista(self, cycle_path):
        is_dry_run = self.bot_data.get("dry_run", True)
        moeda_inicial_rota = cycle_path[0]
        
        try:
            saldos_pre_trade = await self.api_client.get_spot_balances()
            investimento_inicial = Decimal(str(saldos_pre_trade.get(moeda_inicial_rota, {}).get('free', '0')))
            
            if is_dry_run:
                profit_rota = next((x["profit"] for x in self.simulacao_data if x["cycle"] == cycle_path), None)
                await self.bot.send_message(ADMIN_CHAT_ID, f"🎯 **Alvo Realista na Mira (Simulação)**\n"
                                            f"Rota: `{" -> ".join(cycle_path)}`\n"
                                            f"Investimento: `{investimento_inicial:.4f} {moeda_inicial_rota}`\n"
                                            f"Lucro Líquido Realista: `{(profit_rota if profit_rota is not None else Decimal('0')):.4f}%`", parse_mode="Markdown")
                return

            await self.bot.send_message(ADMIN_CHAT_ID, f"🚀 **Iniciando Trade REAL...**\n"
                                        f"Rota: `{" -> ".join(cycle_path)}`\n"
                                        f"Investimento Planejado: `{investimento_inicial:.4f} {moeda_inicial_rota}`", parse_mode="Markdown")
            
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                
                saldos_step = await self.api_client.get_spot_balances()
                saldo_a_negociar = Decimal(str(saldos_step.get(coin_from, {}).get('free', '0')))
                
                if saldo_a_negociar <= 0:
                    await self.bot.send_message(ADMIN_CHAT_ID, f"❌ **FALHA CRÍTICA (Passo {i+1})**\nSaldo de `{coin_from}` é zero. Abortando.", parse_mode="Markdown")
                    return
                    
                pair_info = self.pair_rules.get(pair_id)
                if not pair_info:
                    await self.bot.send_message(ADMIN_CHAT_ID, f"❌ **FALHA CRÍTICA (Passo {i+1})**\nNão foi possível encontrar as regras para o par `{pair_id}`. Abortando.", parse_mode="Markdown")
                    return
                
                amount_prec = pair_info['precision']['amount'] if 'precision' in pair_info and 'amount' in pair_info['precision'] else 8
                quantizer = Decimal(f"1e-{amount_prec}")
                
                amount_to_trade = (saldo_a_negociar * MARGEM_DE_SEGURANCA).quantize(quantizer, rounding=ROUND_DOWN)
                
                if 'limits' in pair_info and 'amount' in pair_info['limits'] and pair_info['limits']['amount'] and 'min' in pair_info['limits']['amount'] and amount_to_trade < Decimal(str(pair_info['limits']['amount']['min'])):
                    await self.bot.send_message(ADMIN_CHAT_ID, f"⚠️ Passo {i+1}: amount ({amount_to_trade}) abaixo do mínimo do par ({pair_info['limits']['amount']['min']}). Abortando.", parse_mode="Markdown")
                    return

                if amount_to_trade <= 0:
                    await self.bot.send_message(ADMIN_CHAT_ID, f"❌ **FALHA CRÍTICA (Passo {i+1})**\nSaldo de `{coin_from}` (`{saldo_a_negociar}`) é muito pequeno. Abortando.", parse_mode="Markdown")
                    return

                await self.bot.send_message(ADMIN_CHAT_ID, f"⏳ Passo {i+1}/{len(cycle_path)-1}: Negociando `{amount_to_trade} {coin_from}` para `{coin_to}` no par `{pair_id}`.", parse_mode="Markdown")
                
                if side == 'buy':
                    order_result = await self.api_client.create_market_buy_order(pair_id, float(amount_to_trade))
                else:
                    order_result = await self.api_client.create_market_sell_order(pair_id, float(amount_to_trade))

                if isinstance(order_result, ccxt.ExchangeError):
                    if i > 0:
                        moeda_destino = coin_to
                        pair_to_monitor = pair_id
                        self.stop_loss_monitoring_task = asyncio.create_task(self._monitorar_stop_loss(moeda_destino, investimento_inicial, pair_to_monitor))
                    await self.bot.send_message(ADMIN_CHAT_ID, f"❌ **FALHA NO PASSO {i+1} ({pair_id})**\n**Motivo:** `{order_result.args[0]}`\n**ALERTA:** Saldo em `{coin_from}` pode estar preso!", parse_mode="Markdown")
                    return
                await asyncio.sleep(2)
            
            await self.bot.send_message(ADMIN_CHAT_ID, f"✅ Trade Concluído com Sucesso!", parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Erro durante a execução do trade realista: {e}", exc_info=True)
            await self.bot.send_message(ADMIN_CHAT_ID, f"❌ Erro crítico durante o trade: `{e}`", parse_mode="Markdown")
        finally:
            if self.stop_loss_monitoring_task:
                self.stop_loss_monitoring_task.cancel()
                self.stop_loss_monitoring_task = None
            if self.trade_lock.locked(): self.trade_lock.release()
            await self.bot.send_message(ADMIN_CHAT_ID, f"Trade para rota `{" -> ".join(cycle_path)}` finalizado.", parse_mode="Markdown")

    async def reconstruir_rotas(self):
        if not self.routes_ready.is_set() and len(self.all_cycles) > 0:
            await self.bot.send_message(ADMIN_CHAT_ID, "Aguarde, uma construção de rotas já está em andamento.", parse_mode="Markdown")
            return
        self.routes_ready.clear()
        await self.bot.send_message(ADMIN_CHAT_ID, "🔄 Reconstruindo rotas com a nova profundidade em segundo plano...", parse_mode="Markdown")
        asyncio.create_task(self.build_routes_background())

    async def gerar_relatorio_detalhado(self, cycle_path: list):
        return "⚠️ A função de relatório detalhado não está implementada nesta versão."

# --- 4. TELEGRAM INTERFACE ---
# A instância do bot deve ser criada antes dos handlers
bot = AsyncTeleBot(TELEGRAM_TOKEN)

@bot.message_handler(commands=['start'])
async def start_command(message):
    await bot.reply_to(message, "Olá! Gênesis v17.9 (OKX) online. Use /status para começar.")

@bot.message_handler(commands=['status'])
async def status_command(message):
    engine = bot.engine
    status_text = "▶️ Rodando" if engine.bot_data.get('is_running') else "⏸️ Pausado"
    if engine.bot_data.get('is_running') and engine.trade_lock.locked():
        status_text = "▶️ Rodando (Processando Alvo)"
    
    if not engine.routes_ready.is_set():
        status_text = "⏳ Construindo Rotas..."

    msg = (f"**📊 Painel de Controle - Gênesis v17.9 (OKX)**\n\n"
           f"**Estado:** `{status_text}`\n"
           f"**Modo:** `{'Simulação' if engine.bot_data.get('dry_run') else '🔴 REAL'}`\n"
           f"**Estratégia:** `Juros Compostos`\n"
           f"**Lucro Mínimo (Líquido Realista):** `{engine.bot_data.get('min_profit')}%`\n"
           f"**Profundidade de Busca:** `{engine.bot_data.get('max_route_depth')}`")
    await bot.send_message(message.chat.id, msg, parse_mode='Markdown')

@bot.message_handler(commands=['radar'])
async def radar_command(message):
    engine: GenesisEngine = bot.engine
    if not engine.routes_ready.is_set():
        await bot.reply_to(message, "📡 O Radar está aguardando a construção das rotas ser finalizada.")
        return
    if not engine.simulacao_data:
        await bot.reply_to(message, "📡 Radar do Caçador (OKX): Nenhuma simulação foi concluída ainda.")
        return
    oportunidades_reais = sorted([op for op in engine.simulacao_data if op['profit'] > 0], key=lambda x: x['profit'], reverse=True)
    if not oportunidades_reais:
        await bot.reply_to(message, "🔎 Nenhuma oportunidade de lucro acima de 0% foi encontrada no momento.")
        return
    top_5_results = oportunidades_reais[:5]
    msg = "📡 **Radar do Caçador (Top 5 Alvos - OKX)**\n\n"
    for result in top_5_results:
        rota_fmt = ' -> '.join(result['cycle'])
        msg += f"**- Rota:** `{rota_fmt}`\n"
        msg += f"  **Lucro Líquido Realista:** `🔼 {result['profit']:.4f}%`\n\n"
    await bot.send_message(message.chat.id, msg, parse_mode='Markdown')

@bot.message_handler(commands=['debug_radar'])
async def debug_radar_command(message):
    await bot.reply_to(message, "⚠️ A função de relatório detalhado não está implementada nesta versão.")

@bot.message_handler(commands=['diagnostico'])
async def diagnostico_command(message):
    engine: GenesisEngine = bot.engine
    if not engine:
        await bot.reply_to(message, "O motor ainda não foi inicializado.")
        return
    uptime_seconds = time.time() - engine.stats['start_time']
    m, s = divmod(uptime_seconds, 60)
    h, m = divmod(m, 60)
    uptime_str = f"{int(h)}h {int(m)}m {int(s)}s"
    
    status_motor = "PAUSADO"
    if engine.bot_data.get('is_running'):
        status_motor = "AGUARDANDO ROTAS" if not engine.routes_ready.is_set() else "ATIVO"

    tempo_desde_ultimo_ciclo = time.time() - engine.stats['ultimo_ciclo_timestamp'] if engine.stats['ultimo_ciclo_timestamp'] > engine.stats['start_time'] else 0

    msg = (f"**🩺 Diagnóstico Interno - Gênesis v17.9 (OKX)**\n\n"
           f"**Ativo há:** `{uptime_str}`\n"
           f"**Motor Principal:** `{status_motor}`\n"
           f"**Trava de Trade:** `{'BLOQUEADO (em trade)' if engine.trade_lock.locked() else 'LIVRE'}`\n"
           f"**Último Ciclo de Verificação:** `{tempo_desde_ultimo_ciclo:.1f} segundos atrás`\n\n"
           f"--- **Estatísticas da Sessão** ---\n"
           f"**Rotas Encontradas:** `{len(engine.all_cycles) if engine.routes_ready.is_set() else 'Calculando...'}`\n"
           f"**Ciclos de Verificação Totais:** `{engine.stats['ciclos_verificacao_total']}`\n"
           f"**Rotas Sobreviventes (Simulação Real):** `{engine.stats['rotas_sobreviventes_total']}`\n")
    await bot.send_message(message.chat.id, msg, parse_mode='Markdown')

@bot.message_handler(commands=['saldo'])
async def saldo_command(message):
    engine: GenesisEngine = bot.engine
    if not engine:
        await bot.reply_to(message, "A conexão com a exchange ainda não foi estabelecida.")
        return
    await bot.reply_to(message, "Buscando saldos na OKX...")
    try:
        saldos = await engine.api_client.get_spot_balances()
        if not saldos or isinstance(saldos, ccxt.ExchangeError):
            await bot.reply_to(message, f"❌ Erro ao buscar saldos: {saldos.args[0] if isinstance(saldos, ccxt.ExchangeError) else 'Resposta vazia'}")
            return
        msg = "**💰 Saldos Atuais (Spot OKX)**\n\n"
        non_zero_saldos = {c: s['free'] for c, s in saldos['free'].items() if Decimal(str(s)) > 0}
        if not non_zero_saldos:
            await bot.reply_to(message, "Nenhum saldo encontrado.")
            return
        for moeda, saldo in non_zero_saldos.items():
            msg += f"**{moeda}:** `{Decimal(str(saldo))}`\n"
        await bot.send_message(message.chat.id, msg, parse_mode='Markdown')
    except Exception as e:
        await bot.reply_to(message, f"❌ Erro ao buscar saldos: `{e}`")

@bot.message_handler(commands=['modo_real'])
async def modo_real_command(message):
    bot.engine.bot_data['dry_run'] = False
    await bot.reply_to(message, "🔴 **MODO REAL ATIVADO (OKX).**")
    await status_command(message)

@bot.message_handler(commands=['modo_simulacao'])
async def modo_simulacao_command(message):
    bot.engine.bot_data['dry_run'] = True
    await bot.reply_to(message, "🔵 **Modo Simulação Ativado (OKX).**")
    await status_command(message)

@bot.message_handler(commands=['setlucro'])
async def setlucro_command(message):
    try:
        val = message.text.split()[1]
        bot.engine.bot_data['min_profit'] = Decimal(val)
        await bot.reply_to(message, f"✅ Lucro mínimo (OKX) definido para **{val}%**.")
    except (IndexError, TypeError, ValueError):
        await bot.reply_to(message, "⚠️ Uso: `/setlucro 0.01`")

@bot.message_handler(commands=['setdepth'])
async def setdepth_command(message):
    try:
        new_depth = int(message.text.split()[1])
        if 2 <= new_depth <= 6:
            bot.engine.bot_data['max_route_depth'] = new_depth
            await bot.engine.reconstruir_rotas()
        else:
            await bot.reply_to(message, "⚠️ A profundidade de busca deve ser um número entre 2 e 6.")
    except (IndexError, TypeError, ValueError):
        await bot.reply_to(message, "⚠️ Uso: `/setdepth 4`")
    
@bot.message_handler(commands=['pausar'])
async def pausar_command(message):
    bot.engine.bot_data['is_running'] = False
    await bot.reply_to(message, "⏸️ **Bot (OKX) pausado.**")
    await status_command(message)

@bot.message_handler(commands=['retomar'])
async def retomar_command(message):
    bot.engine.bot_data['is_running'] = True
    await bot.reply_to(message, "✅ **Bot (OKX) retomado.**")
    await status_command(message)

# Uma única função main para iniciar o bot e as tarefas
async def main():
    if not all([OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSWORD, TELEGRAM_TOKEN, ADMIN_CHAT_ID]):
        logger.critical("❌ Falha crítica: Variáveis de ambiente incompletas.")
        return

    # A instância do motor é anexada ao bot para acesso global nos handlers
    bot.engine = GenesisEngine(bot)

    logger.info("Iniciando motor Gênesis v17.9 (OKX)...")
    try:
        await bot.send_message(ADMIN_CHAT_ID, "🤖 Gênesis v17.9 (OKX) iniciado. Construindo rotas em segundo plano...")
        logger.info("✅ Mensagem de inicialização enviada com sucesso para o Telegram.")
    except ApiTelegramException as e:
        logger.critical(f"❌ Falha crítica ao enviar mensagem inicial. O bot será encerrado. Erro: {e}")
        sys.exit(1)
    
    # Inicia as tarefas de fundo de forma assíncrona
    asyncio.create_task(bot.engine.build_routes_background())
    asyncio.create_task(bot.engine.verificar_oportunidades())
    
    logger.info("Motor e tarefas de fundo iniciadas. Iniciando polling do Telebot...")
    await bot.polling()

if __name__ == "__main__":
    # Esta é a maneira correta de iniciar o loop de eventos
    asyncio.run(main())
