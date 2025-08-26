# bot_okx.py
# Gênesis v17.9 - Adaptado para OKX com a lógica comprovada da Gate.io
import os
import asyncio
import logging
from decimal import Decimal, getcontext, ROUND_DOWN
import time
import uuid
import sys

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

# --- Pilares da Estratégia v17.9 ---
TAXA_OPERACAO = Decimal("0.001") # OKX TAKER FEE
MIN_PROFIT_DEFAULT = Decimal("0.001")
MARGEM_DE_SEGURANCA = Decimal("0.995")
MOEDAS_BASE_OPERACIONAL = ["USDT", "USDC"]
MAX_ROUTE_DEPTH = 4
ORDER_BOOK_DEPTH = 100  # <<-- CORREÇÃO: Restaurado para 100, pois a lógica mudou

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

def _quantizer_from_decimal(d: Decimal, fallback_places: int = 6):
    if not d:
        return Decimal(f"1e-{fallback_places}")
    places = max(-d.as_tuple().exponent, 0)
    return Decimal("1").scaleb(-places)

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

    async def get_all_pairs(self):
        return self.markets

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

# --- 3. GÊNESIS ENGINE v17.9 (OKX) ---
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
        self.graph = {}
        self.rotas_monitoradas = []
        self.simulacao_data = []
        self.trade_lock = asyncio.Lock()
        self.stats = {
            "start_time": time.time(),
            "ciclos_verificacao_total": 0,
            "rotas_sobreviventes_total": 0,
            "ultimo_ciclo_timestamp": time.time()
        }
        self.all_pairs_data = None

    async def inicializar(self):
        logger.info("Gênesis v17.9 (OKX): Iniciando...")
        self.all_pairs_data = await self.api_client.load_markets()
        if not self.all_pairs_data or isinstance(self.all_pairs_data, ccxt.ExchangeError):
            logger.critical("Gênesis: Não foi possível obter os pares da OKX. Verifique as chaves da API e a conexão.")
            return

        for pair_id, pair_data in self.all_pairs_data.items():
            try:
                if pair_data.get('active'):
                    base, quote = pair_data['base'], pair_data['quote']
                    self.pair_rules[pair_id] = pair_data
                    if base not in self.graph: self.graph[base] = []
                    if quote not in self.graph: self.graph[quote] = []
                    self.graph[base].append(quote)
                    self.graph[quote].append(base)
            except Exception as e:
                logger.warning(f"Erro processando pair_data {pair_id}: {e}")

        logger.info(f"Gênesis: Mapa construído. Buscando rotas de até {self.bot_data["max_route_depth"]} passos...")
        self.rotas_monitoradas = []
        for start_node in MOEDAS_BASE_OPERACIONAL:
            if start_node in self.graph:
                def encontrar_ciclos_dfs(u, path, depth):
                    if depth > self.bot_data["max_route_depth"]: return
                    for v in self.graph.get(u, []):
                        if v == start_node and len(path) > 2:
                            self.rotas_monitoradas.append(path + [v])
                        elif v not in path:
                            encontrar_ciclos_dfs(v, path + [v], depth + 1)
                encontrar_ciclos_dfs(start_node, [start_node], 1)

        self.rotas_monitoradas = list(set(tuple(r) for r in self.rotas_monitoradas))
        logger.info(f"Gênesis: {len(self.rotas_monitoradas)} rotas únicas encontradas.")
        self.bot_data["total_ciclos"] = len(self.rotas_monitoradas)

    def _get_pair_details(self, coin_from, coin_to):
        pair_v1 = f"{coin_from}/{coin_to}"
        if pair_v1 in self.pair_rules: return pair_v1, "sell"
        pair_v2 = f"{coin_to}/{coin_from}"
        if pair_v2 in self.pair_rules: return pair_v2, "buy"
        return None, None

    async def _simular_realidade(self, cycle_path, investimento_inicial):
        try:
            # <<-- CORREÇÃO: A simulação agora busca o livro de ordens em tempo real
            # para cada par da rota, garantindo que o cache de memória seja mínimo.
            valor_simulado = investimento_inicial
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                if not pair_id: return None
                
                pair_info = self.pair_rules.get(pair_id)
                if not pair_info: return None
                
                amount_prec = pair_info['precision']['amount'] if 'precision' in pair_info and 'amount' in pair_info['precision'] else 8
                quantizer = Decimal(f"1e-{amount_prec}")
                
                # Busca o livro de ordens, mas não o armazena no cache global
                order_book = await self.api_client.get_order_book(pair_id)
                if not order_book or isinstance(order_book, ccxt.ExchangeError): return None
                
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
                    if 'limits' in pair_info and 'amount' in pair_info['limits'] and pair_info['limits']['amount'] and 'min' in pair_info['limits']['amount'] and quantidade_comprada < Decimal(pair_info['limits']['amount']['min']): return None
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
                    if 'limits' in pair_info and 'amount' in pair_info['limits'] and pair_info['limits']['amount'] and 'min' in pair_info['limits']['amount'] and valor_simulado < Decimal(pair_info['limits']['amount']['min']): return None
                    valor_simulado = valor_recebido
                valor_simulado *= (1 - TAXA_OPERACAO)
                # Adiciona um pequeno delay entre as chamadas para não sobrecarregar a API
                await asyncio.sleep(0.01)

            if investimento_inicial == 0: return Decimal("0")
            return ((valor_simulado - investimento_inicial) / investimento_inicial) * 100
        except Exception as e:
            logger.error(f"Erro na simulação para a rota {" -> ".join(cycle_path)}: {e}", exc_info=True)
            return None

    async def verificar_oportunidades(self):
        logger.info("Gênesis: Motor \"O Caçador de Migalhas\" (OKX) iniciado.")
        while True:
            if not self.bot_data.get("is_running", True) or self.trade_lock.locked():
                await asyncio.sleep(1)
                continue
            try:
                self.stats["ciclos_verificacao_total"] += 1
                self.stats["ultimo_ciclo_timestamp"] = time.time()
                
                # Obtém os saldos apenas uma vez por ciclo
                saldos = await self.api_client.get_spot_balances()
                if not saldos or isinstance(saldos, ccxt.ExchangeError):
                    await asyncio.sleep(5)
                    continue
                saldo_por_moeda = {c: Decimal(str(saldos.get(c, {}).get('free', '0'))) for c in saldos['free'] if saldos.get(c, {}).get('free')}

                self.simulacao_data = []
                for cycle_path in self.rotas_monitoradas:
                    moeda_inicial_rota = cycle_path[0]
                    if (volume_a_simular := saldo_por_moeda.get(moeda_inicial_rota, Decimal("0"))) > 0:
                        # <<-- CORREÇÃO: Chamando a nova função de simulação que não usa cache
                        if (profit := await self._simular_realidade(cycle_path, volume_a_simular)) is not None:
                            self.simulacao_data.append({"cycle": cycle_path, "profit": profit})
                    # Adiciona um pequeno delay para que o loop não seja muito pesado
                    await asyncio.sleep(0.05)

                oportunidades_reais = sorted([op for op in self.simulacao_data if op["profit"] > self.bot_data["min_profit"]], key=lambda x: x["profit"], reverse=True)
                self.stats["rotas_sobreviventes_total"] += len(oportunidades_reais)

                if oportunidades_reais:
                    async with self.trade_lock:
                        melhor_oportunidade = oportunidades_reais[0]
                        logger.info(f"Gênesis: Oportunidade REALISTA encontrada ({melhor_oportunidade["profit"]:.4f}%).")
                        await self._executar_trade_realista(melhor_oportunidade["cycle"])
            except Exception as e:
                logger.error(f"Gênesis: Erro no loop de verificação: {e}", exc_info=True)
            finally:
                # Tempo de espera entre os ciclos de verificação
                await asyncio.sleep(10)

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
                                            f"Lucro Líquido Realista: `{(profit_rota if profit_rota is not None else Decimal("0")):.4f}%`", parse_mode="Markdown")
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
                
                if 'limits' in pair_info and 'amount' in pair_info['limits'] and pair_info['limits']['amount'] and 'min' in pair_info['limits']['amount'] and amount_to_trade < Decimal(pair_info['limits']['amount']['min']):
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
                    await self.bot.send_message(ADMIN_CHAT_ID, f"❌ **FALHA NO PASSO {i+1} ({pair_id})**\n**Motivo:** `{order_result.args[0]}`\n**ALERTA:** Saldo em `{coin_from}` pode estar preso!", parse_mode="Markdown")
                    return
                await asyncio.sleep(2)
            
            # TODO: Implementar monitoramento de Stop Loss aqui (igual ao do bot da Gate.io)
            await self.bot.send_message(ADMIN_CHAT_ID, f"✅ Trade Concluído com Sucesso!", parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Erro durante a execução do trade realista: {e}", exc_info=True)
            await self.bot.send_message(ADMIN_CHAT_ID, f"❌ Erro crítico durante o trade: `{e}`", parse_mode="Markdown")
        finally:
            if self.trade_lock.locked(): self.trade_lock.release()
            await self.bot.send_message(ADMIN_CHAT_ID, f"Trade para rota `{" -> ".join(cycle_path)}` finalizado.", parse_mode="Markdown")

    async def gerar_relatorio_detalhado(self, cycle_path: list):
        return "⚠️ A função de relatório detalhado não está implementada nesta versão."

# --- 4. TELEGRAM INTERFACE ---
async def start_command(message):
    await bot.reply_to(message, "Olá! Gênesis v17.9 (OKX) online. Use /status para começar.")

async def status_command(message):
    engine = bot.engine
    status_text = "▶️ Rodando" if engine.bot_data.get('is_running') else "⏸️ Pausado"
    if engine.bot_data.get('is_running') and engine.trade_lock.locked():
        status_text = "▶️ Rodando (Processando Alvo)"
    msg = (f"**📊 Painel de Controle - Gênesis v17.9 (OKX)**\n\n"
           f"**Estado:** `{status_text}`\n"
           f"**Modo:** `{'Simulação' if engine.bot_data.get('dry_run') else '🔴 REAL'}`\n"
           f"**Estratégia:** `Juros Compostos`\n"
           f"**Lucro Mínimo (Líquido Realista):** `{engine.bot_data.get('min_profit')}%`\n"
           f"**Profundidade de Busca:** `{engine.bot_data.get('max_route_depth')}`\n"
           f"**Total de Rotas Monitoradas:** `{engine.bot_data.get('total_ciclos', 0)}`")
    await bot.send_message(message.chat.id, msg, parse_mode='Markdown')

async def radar_command(message):
    engine: GenesisEngine = bot.engine
    if not engine or not engine.simulacao_data:
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

async def debug_radar_command(message):
    await bot.reply_to(message, "⚠️ A função de relatório detalhado não está implementada nesta versão.")

async def diagnostico_command(message):
    engine: GenesisEngine = bot.engine
    if not engine:
        await bot.reply_to(message, "O motor ainda não foi inicializado.")
        return
    uptime_seconds = time.time() - engine.stats['start_time']
    m, s = divmod(uptime_seconds, 60)
    h, m = divmod(m, 60)
    uptime_str = f"{int(h)}h {int(m)}m {int(s)}s"
    tempo_desde_ultimo_ciclo = time.time() - engine.stats['ultimo_ciclo_timestamp']
    msg = (f"**🩺 Diagnóstico Interno - Gênesis v17.9 (OKX)**\n\n"
           f"**Ativo há:** `{uptime_str}`\n"
           f"**Motor Principal:** `{'ATIVO' if engine.bot_data.get('is_running') else 'PAUSADO'}`\n"
           f"**Trava de Trade:** `{'BLOQUEADO (em trade)' if engine.trade_lock.locked() else 'LIVRE'}`\n"
           f"**Último Ciclo de Verificação:** `{tempo_desde_ultimo_ciclo:.1f} segundos atrás`\n\n"
           f"--- **Estatísticas Totais da Sessão** ---\n"
           f"**Ciclos de Verificação Totais:** `{engine.stats['ciclos_verificacao_total']}`\n"
           f"**Rotas Sobreviventes (Simulação Real):** `{engine.stats['rotas_sobreviventes_total']}`\n")
    await bot.send_message(message.chat.id, msg, parse_mode='Markdown')

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

async def modo_real_command(message):
    bot.engine.bot_data['dry_run'] = False
    await bot.reply_to(message, "🔴 **MODO REAL ATIVADO (OKX).**")
    await status_command(message)

async def modo_simulacao_command(message):
    bot.engine.bot_data['dry_run'] = True
    await bot.reply_to(message, "🔵 **Modo Simulação Ativado (OKX).**")
    await status_command(message)

async def setlucro_command(message):
    try:
        val = message.text.split()[1]
        bot.engine.bot_data['min_profit'] = Decimal(val)
        await bot.reply_to(message, f"✅ Lucro mínimo (OKX) definido para **{val}%**.")
    except (IndexError, TypeError, ValueError):
        await bot.reply_to(message, "⚠️ Uso: `/setlucro 0.01`")

async def setdepth_command(message):
    try:
        new_depth = int(message.text.split()[1])
        if 2 <= new_depth <= 6:
            bot.engine.bot_data['max_route_depth'] = new_depth
            await bot.reply_to(message, f"✅ Profundidade de busca (OKX) definida para **{new_depth}**. Reconstruindo rotas...")
            await bot.engine.inicializar()
        else:
            await bot.reply_to(message, "⚠️ A profundidade de busca deve ser um número entre 2 e 6.")
    except (IndexError, TypeError, ValueError):
        await bot.reply_to(message, "⚠️ Uso: `/setdepth 4`")
    
async def pausar_command(message):
    bot.engine.bot_data['is_running'] = False
    await bot.reply_to(message, "⏸️ **Bot (OKX) pausado.**")
    await status_command(message)

async def retomar_command(message):
    bot.engine.bot_data['is_running'] = True
    await bot.reply_to(message, "✅ **Bot (OKX) retomado.**")
    await status_command(message)

async def main():
    if not all([OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSWORD, TELEGRAM_TOKEN, ADMIN_CHAT_ID]):
        logger.critical("❌ Falha crítica: Variáveis de ambiente incompletas.")
        return

    global bot
    bot = AsyncTeleBot(TELEGRAM_TOKEN)
    
    bot.engine = GenesisEngine(bot)
    
    bot.message_handler(commands=['start'])(start_command)
    bot.message_handler(commands=['status'])(status_command)
    bot.message_handler(commands=['radar'])(radar_command)
    bot.message_handler(commands=['debug_radar'])(debug_radar_command)
    bot.message_handler(commands=['diagnostico'])(diagnostico_command)
    bot.message_handler(commands=['saldo'])(saldo_command)
    bot.message_handler(commands=['modo_real'])(modo_real_command)
    bot.message_handler(commands=['modo_simulacao'])(modo_simulacao_command)
    bot.message_handler(commands=['setlucro'])(setlucro_command)
    bot.message_handler(commands=['setdepth'])(setdepth_command)
    bot.message_handler(commands=['pausar'])(pausar_command)
    bot.message_handler(commands=['retomar'])(retomar_command)

    logger.info("Iniciando motor Gênesis v17.9 (OKX)...")
    try:
        await bot.send_message(ADMIN_CHAT_ID, "🤖 Gênesis v17.9 (OKX) iniciado. Carregando dados...")
    except ApiTelegramException as e:
        logger.error(f"Não foi possível enviar mensagem inicial. Verifique o CHAT_ID e o TOKEN do Telegram: {e}")
        return

    await bot.engine.inicializar()
    
    asyncio.create_task(bot.engine.verificar_oportunidades())
    
    logger.info("Motor e tarefas de fundo iniciadas. Iniciando polling do Telebot...")
    await bot.polling()

if __name__ == "__main__":
    asyncio.run(main())
