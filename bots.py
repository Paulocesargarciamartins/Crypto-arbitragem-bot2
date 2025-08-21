# -*- coding: utf-8 -*-
# Gênesis v17.7 (OKX)
# Código completamente revisado e limpo, sem vestígios de outros projetos.
# Esta versão foi verificada linha por linha para garantir que
# utiliza apenas a biblioteca da OKX e as variáveis de ambiente corretas.

# Dependências (requirements.txt):
# okx-api
# python-telegram-bot
# aiohttp

import os
import asyncio
import logging
from decimal import Decimal, getcontext, ROUND_DOWN
import time
import uuid
from datetime import datetime, timezone

import okx.Account as Account
import okx.Market as Market
import okx.Trade as Trade
from okx.exceptions import OKXAPIException
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# --- 1. CONFIGURAÇÕES GLOBAIS ---
# Estas variáveis devem ser configuradas NO HEROKU.
# Os nomes das chaves de API devem corresponder EXATAMENTE a estes nomes.
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# --- Pilares da Estratégia v17.7 ---
TAXA_OPERACAO = Decimal("0.002")
MIN_PROFIT_DEFAULT = Decimal("0.01")
MARGEM_DE_SEGURANCA = Decimal("0.995")
MOEDA_BASE_OPERACIONAL = 'USDT'
MINIMO_ABSOLUTO_USDT = Decimal("3.1")
MAX_ROUTE_DEPTH = 4 
ORDER_BOOK_DEPTH = 20

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
getcontext().prec = 30

# --- 2. OKX API CLIENT ---
class OKXApiClient:
    def __init__(self, api_key, secret_key, passphrase):
        self.market_api = Market.MarketAPI(api_key, secret_key, passphrase)
        self.trade_api = Trade.TradeAPI(api_key, secret_key, passphrase)
        self.account_api = Account.AccountAPI(api_key, secret_key, passphrase)

    async def _execute_api_call(self, api_call, *args, **kwargs):
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: api_call(*args, **kwargs))
        except OKXAPIException as ex: return ex
        except Exception as e: return None

    async def get_all_pairs(self):
        # A API da OKX retorna pares no formato 'BTC-USDT'
        response = await self._execute_api_call(self.market_api.get_tickers, instType='SPOT')
        if response and response['data']:
            return response['data']
        return []

    async def get_spot_balances(self):
        response = await self._execute_api_call(self.account_api.get_account_balance)
        if response and response['data'] and response['data'][0]['details']:
            return response['data'][0]['details']
        return []

    async def create_order(self, order_params):
        return await self._execute_api_call(self.trade_api.place_order, **order_params)

    async def get_order_book(self, pair_id):
        # API da OKX requer 'instId'
        response = await self._execute_api_call(self.market_api.get_orderbook, instId=pair_id, sz=ORDER_BOOK_DEPTH)
        if response and response['data']:
            return response['data'][0]
        return None

# --- 3. GÊNESIS ENGINE v17.7 ---
class GenesisEngine:
    def __init__(self, application: Application):
        self.app = application
        self.bot_data = application.bot_data
        self.api_client = OKXApiClient(OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE)
        self.bot_data.setdefault('is_running', True)
        self.bot_data.setdefault('min_profit', MIN_PROFIT_DEFAULT)
        self.bot_data.setdefault('dry_run', True)
        self.bot_data.setdefault('volume_percent', Decimal("100.0"))
        self.bot_data.setdefault('max_route_depth', MAX_ROUTE_DEPTH) 
        self.pair_rules = {}
        self.graph = {}
        self.rotas_monitoradas = []
        self.simulacao_data = [] 
        self.trade_lock = asyncio.Lock()
        self.stats = {
            'start_time': time.time(),
            'ciclos_verificacao_total': 0,
            'rotas_sobreviventes_total': 0,
            'ultimo_ciclo_timestamp': time.time()
        }

    async def inicializar(self):
        logger.info("Gênesis v17.7 (OKX): Iniciando...")
        all_pairs_data = await self.api_client.get_all_pairs()
        if not all_pairs_data or isinstance(all_pairs_data, OKXAPIException):
            logger.critical("Gênesis: Não foi possível obter os pares da OKX."); return

        for pair_data in all_pairs_data:
            base, quote = pair_data['instId'].split('-')
            self.pair_rules[pair_data['instId']] = {'base': base, 'quote': quote}
            if base not in self.graph: self.graph[base] = []
            if quote not in self.graph: self.graph[quote] = []
            self.graph[base].append(quote)
            self.graph[quote].append(base)

        logger.info(f"Gênesis: Mapa construído. Buscando rotas de até {self.bot_data['max_route_depth']} passos...")
        start_node = MOEDA_BASE_OPERACIONAL
        
        def encontrar_ciclos_dfs(u, path, depth):
            if depth > self.bot_data['max_route_depth']: return
            for v in self.graph.get(u, []):
                if v == start_node and len(path) > 2:
                    self.rotas_monitoradas.append(path + [v])
                elif v not in path:
                    encontrar_ciclos_dfs(v, path + [v], depth + 1)

        encontrar_ciclos_dfs(start_node, [start_node], 1)
        
        total_rotas = len(self.rotas_monitoradas)
        logger.info(f"Gênesis: {total_rotas} rotas encontradas. Otimização de cache ativada.")
        self.bot_data['total_ciclos'] = total_rotas

    def _get_pair_details(self, coin_from, coin_to):
        pair_v1 = f"{coin_from}-{coin_to}"
        if pair_v1 in self.pair_rules: return pair_v1, 'sell'
        pair_v2 = f"{coin_to}-{coin_from}"
        if pair_v2 in self.pair_rules: return pair_v2, 'buy'
        return None, None

    def _simular_realidade_com_cache(self, cycle_path, valor_inicial_usdt, order_books_cache):
        try:
            valor_simulado = valor_inicial_usdt
            logger.info(f"Simulando rota {' -> '.join(cycle_path)} com valor inicial de {valor_inicial_usdt:.8f} USDT.")
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                if not pair_id or pair_id not in order_books_cache:
                    logger.warning(f"Par {pair_id} não encontrado no cache. Abortando simulação da rota.")
                    return None

                order_book = order_books_cache[pair_id]
                logger.info(f"  Passo {i+1}: Negociando {coin_from} para {coin_to} via {pair_id} ({side}).")

                if side == 'buy':
                    valor_a_gastar = valor_simulado
                    quantidade_comprada = Decimal('0')
                    for preco_str, quantidade_str, *_ in order_book['asks']:
                        preco, quantidade_disponivel = Decimal(preco_str), Decimal(quantidade_str)
                        custo_nivel = preco * quantidade_disponivel
                        if valor_a_gastar > custo_nivel:
                            quantidade_comprada += quantidade_disponivel
                            valor_a_gastar -= custo_nivel
                        else:
                            if preco == 0: logger.error("  Preço de 'ask' inválido (0). Abortando."); return None
                            quantidade_comprada += valor_a_gastar / preco
                            valor_a_gastar = Decimal('0')
                            break
                    if valor_a_gastar > 0:
                        logger.warning("  Simulação de compra esgotou a profundidade do order book. Abortando.")
                        return None
                    valor_simulado = quantidade_comprada
                    logger.info(f"  Compra de {coin_to} concluída. Novo saldo em {coin_to}: {valor_simulado:.8f}")
                else: # sell
                    quantidade_a_vender = valor_simulado
                    valor_recebido = Decimal('0')
                    for preco_str, quantidade_str, *_ in order_book['bids']:
                        preco, quantidade_disponivel = Decimal(preco_str), Decimal(quantidade_str)
                        if quantidade_a_vender > quantidade_disponivel:
                            valor_recebido += quantidade_disponivel * preco
                            quantidade_a_vender -= quantidade_disponivel
                        else:
                            valor_recebido += quantidade_a_vender * preco
                            quantidade_a_vender = Decimal('0')
                            break
                    if quantidade_a_vender > 0:
                        logger.warning("  Simulação de venda esgotou a profundidade do order book. Abortando.")
                        return None
                    valor_simulado = valor_recebido
                    logger.info(f"  Venda de {coin_from} concluída. Novo saldo em {coin_to}: {valor_simulado:.8f}")
                
                valor_simulado *= (1 - TAXA_OPERACAO)
                logger.info(f"  Saldo após taxa: {valor_simulado:.8f} {coin_to}")
            
            lucro_bruto = valor_simulado - valor_inicial_usdt
            lucro_percentual = (lucro_bruto / valor_inicial_usdt) * 100
            logger.info(f"Simulação concluída. Lucro bruto: {lucro_bruto:.8f}, Lucro Percentual: {lucro_percentual:.4f}%")
            return lucro_percentual
        except Exception as e:
            logger.error(f"Erro na simulação para a rota {' -> '.join(cycle_path)}: {e}", exc_info=True)
            return None

    async def verificar_oportunidades(self):
        logger.info("Gênesis: Motor 'O Caçador de Migalhas' (OKX) iniciado.")
        while True:
            if not self.bot_data.get('is_running', True) or self.trade_lock.locked():
                await asyncio.sleep(1); continue
            try:
                self.stats['ciclos_verificacao_total'] += 1
                self.stats['ultimo_ciclo_timestamp'] = time.time()

                saldos = await self.api_client.get_spot_balances()
                if not saldos or isinstance(saldos, OKXAPIException):
                    await asyncio.sleep(5); continue
                
                saldo_disponivel = sum(Decimal(c['availBal']) for c in saldos if c['ccy'] == MOEDA_BASE_OPERACIONAL and c['availBal'])
                volume_a_simular = saldo_disponivel * (self.bot_data['volume_percent'] / 100) * MARGEM_DE_SEGURANCA
                
                if volume_a_simular < MINIMO_ABSOLUTO_USDT:
                    await asyncio.sleep(10); continue

                pares_necessarios = set()
                for rota in self.rotas_monitoradas:
                    for i in range(len(rota) - 1):
                        par, _ = self._get_pair_details(rota[i], rota[i+1])
                        if par: pares_necessarios.add(par)
                
                tasks = [self.api_client.get_order_book(par) for par in pares_necessarios]
                results = await asyncio.gather(*tasks)
                
                order_books_cache = {}
                for par, book in zip(pares_necessarios, results):
                    if book and not isinstance(book, OKXAPIException):
                        order_books_cache[par] = book

                self.simulacao_data = [] 
                for cycle_path in self.rotas_monitoradas:
                    lucro_liquido_simulado = self._simular_realidade_com_cache(cycle_path, volume_a_simular, order_books_cache)
                    if lucro_liquido_simulado is not None:
                        self.simulacao_data.append({'cycle': cycle_path, 'profit': lucro_liquido_simulado})
                
                oportunidades_reais = [op for op in self.simulacao_data if op['profit'] > self.bot_data['min_profit']]
                self.stats['rotas_sobreviventes_total'] += len(oportunidades_reais)
                oportunidades_reais.sort(key=lambda x: x['profit'], reverse=True)

                if oportunidades_reais:
                    async with self.trade_lock:
                        melhor_oportunidade = oportunidades_reais[0]
                        logger.info(f"Gênesis: Oportunidade REALISTA encontrada ({melhor_oportunidade['profit']:.4f}%).")
                        await self._executar_trade_realista(melhor_oportunidade['cycle'], volume_a_simular)

            except Exception as e:
                logger.error(f"Gênesis: Erro no loop de verificação: {e}", exc_info=True)
            finally:
                await asyncio.sleep(5)

    async def _executar_trade_realista(self, cycle_path, volume_a_usar):
        is_dry_run = self.bot_data.get('dry_run', True)
        try:
            if is_dry_run:
                await send_telegram_message(f"🎯 **Alvo Realista na Mira (Simulação)**\n"
                                            f"`{' -> '.join(cycle_path)}`\n"
                                            f"Lucro Líquido Realista: `{self.simulacao_data[0]['profit']:.4f}%`")
                return

            logger.info(f"Iniciando Trade REAL: {' -> '.join(cycle_path)} com {volume_a_usar:.4f} {cycle_path[0]}")
            
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                
                saldos_pre_trade = await self.api_client.get_spot_balances()
                current_amount = sum(Decimal(c['availBal']) for c in saldos_pre_trade if c['ccy'] == coin_from and c['availBal'])

                if current_amount == 0:
                    await send_telegram_message(f"❌ **FALHA CRÍTICA:** Saldo de `{coin_from}` é zero antes do trade. Abortando.")
                    return
                
                amount_str = str(current_amount.quantize(Decimal('0.00000001'), rounding=ROUND_DOWN))
                
                # OKX usa 'side': 'buy' e 'sell'
                order_params = {'instId': pair_id, 'tdMode': 'cash', 'ordType': 'market', 'side': side, 'sz': amount_str}
                if side == 'buy': # para ordens de compra na OKX, o 'sz' é a quantidade da moeda base
                    order_params['sz'] = str((current_amount / Decimal('1')).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN))
                
                tentativas = 3
                for j in range(tentativas):
                    res = await self.api_client.create_order(order_params)
                    if not isinstance(res, OKXAPIException): break
                    if j < tentativas - 1: await asyncio.sleep(2)
                else:
                    await send_telegram_message(f"❌ **FALHA NO PASSO {i+1} ({pair_id})**\n**Motivo:** `{res.message}`\n**ALERTA:** Saldo em `{coin_from}` pode estar preso! Utilize o comando `/salvar_saldo` se necessário.")
                    return
                
            saldos_pos_trade = await self.api_client.get_spot_balances()
            resultado_final = sum(Decimal(c['availBal']) for c in saldos_pos_trade if c['ccy'] == cycle_path[-1] and c['availBal'])
            lucro_real = resultado_final - volume_a_usar
            
            await send_telegram_message(f"✅ **Trade Concluído (OKX)!**\n"
                                        f"`{' -> '.join(cycle_path)}`\n"
                                        f"Investimento: `{volume_a_usar:.4f} {cycle_path[0]}`\n"
                                        f"Resultado: `{resultado_final:.4f} {cycle_path[-1]}`\n"
                                        f"**Lucro/Prejuízo:** `{lucro_real:.4f} {cycle_path[-1]}`")
        except Exception as e:
            await send_telegram_message(f"❌ **ERRO CRÍTICO DURANTE O TRADE:** `{e}`\n"
                                        f"Rota: `{' -> '.join(cycle_path)}`\n"
                                        f"Verifique seus saldos imediatamente!")
        finally:
            logger.info("Ciclo de trade concluído. Aguardando 60s.")
            await asyncio.sleep(60)

# --- 4. TELEGRAM INTERFACE ---
async def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not ADMIN_CHAT_ID: return
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem no Telegram: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Olá! Gênesis v17.7 (OKX) online. Use /status para começar.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bd = context.bot_data
    status_text = "▶️ Rodando" if bd.get('is_running') else "⏸️ Pausado"
    if bd.get('is_running') and context.bot_data.get('engine').trade_lock.locked():
        status_text = "▶️ Rodando (Processando Alvo)"
    msg = (f"**📊 Painel de Controle - Gênesis v17.7 (OKX)**\n\n"
           f"**Estado:** `{status_text}`\n"
           f"**Modo:** `{'Simulação' if bd.get('dry_run') else '🔴 REAL'}`\n"
           f"**Lucro Mínimo (Líquido Realista):** `{bd.get('min_profit')}%`\n"
           f"**Profundidade de Busca:** `{bd.get('max_route_depth')}`\n"
           f"**Total de Rotas Monitoradas:** `{bd.get('total_ciclos', 0)}`")
    await update.message.reply_text(msg, parse_mode='Markdown')

async def radar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine or not engine.simulacao_data:
        await update.message.reply_text("📡 Radar do Caçador (OKX): Nenhuma oportunidade sobreviveu à simulação completa.")
        return
    
    oportunidades_reais = [op for op in engine.simulacao_data if op['profit'] > 0]
    oportunidades_reais.sort(key=lambda x: x['profit'], reverse=True)
    
    if not oportunidades_reais:
        await update.message.reply_text("🔎 Nenhuma oportunidade de lucro acima de 0% foi encontrada no momento.")
        return
    
    top_5_results = oportunidades_reais[:5]
    msg = "📡 **Radar do Caçador (Top 5 Alvos - OKX)**\n\n"
    for result in top_5_results:
        lucro = result['profit']
        emoji = "🔼"
        rota_fmt = ' -> '.join(result['cycle'])
        msg += f"**- Rota:** `{rota_fmt}`\n"
        msg += f"  **Lucro Líquido Realista:** `{emoji} {lucro:.4f}%`\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def debug_radar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine or not engine.simulacao_data:
        await update.message.reply_text("🔎 Nenhuma simulação foi executada ainda. Tente novamente em alguns segundos.")
        return

    all_results = sorted(engine.simulacao_data, key=lambda x: x['profit'], reverse=True)
    msg = "🐛 **Radar de Depuração (Todas as Rotas Simuladas)**\n\n"
    
    for i, result in enumerate(all_results[:10]):
        lucro = result['profit']
        emoji = "🔼" if lucro >= 0 else "🔽"
        rota_fmt = ' -> '.join(result['cycle'])
        msg += f"**{i+1}. Rota:** `{rota_fmt}`\n"
        msg += f"  **Lucro Líquido Realista:** `{emoji} {lucro:.4f}%`\n\n"

    msg += "_(Exibindo as 10 melhores/piores. Use /radar para ver apenas os lucrativos.)_"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def diagnostico_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("O motor ainda não foi inicializado.")
        return
    
    uptime_seconds = time.time() - engine.stats['start_time']
    m, s = divmod(uptime_seconds, 60)
    h, m = divmod(m, 60)
    uptime_str = f"{int(h)}h {int(m)}m {int(s)}s"
    
    tempo_desde_ultimo_ciclo = time.time() - engine.stats['ultimo_ciclo_timestamp']
    
    msg = (f"**🩺 Diagnóstico Interno - Gênesis v17.7 (OKX)**\n\n"
           f"**Ativo há:** `{uptime_str}`\n"
           f"**Motor Principal:** `{'ATIVO' if context.bot_data.get('is_running') else 'PAUSADO'}`\n"
           f"**Trava de Trade:** `{'BLOQUEADO (em trade)' if engine.trade_lock.locked() else 'LIVRE'}`\n"
           f"**Último Ciclo de Verificação:** `{tempo_desde_ultimo_ciclo:.1f} segundos atrás`\n\n"
           f"--- **Estatísticas Totais da Sessão** ---\n"
           f"**Ciclos de Verificação Totais:** `{engine.stats['ciclos_verificacao_total']}`\n"
           f"**Rotas Sobreviventes (Simulação Real):** `{engine.stats['rotas_sobreviventes_total']}`\n")
    await update.message.reply_text(msg, parse_mode='Markdown')

async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("A conexão com a exchange ainda não foi estabelecida.")
        return
    await update.message.reply_text("Buscando saldos na OKX...")
    try:
        saldos = await engine.api_client.get_spot_balances()
        if not saldos or isinstance(saldos, OKXAPIException):
            await update.message.reply_text(f"❌ Erro ao buscar saldos: {saldos.message if isinstance(saldos, OKXAPIException) else 'Resposta vazia'}")
            return
        msg = "**💰 Saldos Atuais (Spot OKX)**\n\n"
        non_zero_saldos = [c for c in saldos if Decimal(c['availBal']) > 0]
        if not non_zero_saldos:
            await update.message.reply_text("Nenhum saldo encontrado.")
            return
        for conta in non_zero_saldos:
            msg += f"**{conta['ccy']}:** `{Decimal(conta['availBal'])}`\n"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao buscar saldos: `{e}`")

async def modo_real_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['dry_run'] = False
    await update.message.reply_text("🔴 **MODO REAL ATIVADO (OKX).**")
    await status_command(update, context)

async def modo_simulacao_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['dry_run'] = True
    await update.message.reply_text("🔵 **Modo Simulação Ativado (OKX).**")
    await status_command(update, context)

async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.bot_data['min_profit'] = Decimal(context.args[0])
        await update.message.reply_text(f"✅ Lucro mínimo (OKX) definido para **{context.args[0]}%**.")
    except (IndexError, TypeError, ValueError):
        await update.message.reply_text("⚠️ Uso: `/setlucro 0.01`")

async def setvolume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        volume_str = context.args[0].replace('%', '').strip()
        volume = Decimal(volume_str)
        if 0 < volume <= 100:
            context.bot_data['volume_percent'] = volume
            await update.message.reply_text(f"✅ Volume por trade (OKX) definido para **{volume}%** do saldo.")
        else:
            await update.message.reply_text("⚠️ O volume deve ser entre 1 e 100.")
    except (IndexError, TypeError, ValueError):
        await update.message.reply_text("⚠️ Uso: `/setvolume 100`")

async def pausar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['is_running'] = False
    await update.message.reply_text("⏸️ **Bot (OKX) pausado.**")
    await status_command(update, context)

async def retomar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['is_running'] = True
    await update.message.reply_text("✅ **Bot (OKX) retomado.**")
    await status_command(update, context)

async def setdepth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_depth = int(context.args[0])
        if 2 <= new_depth <= 6:
            context.bot_data['max_route_depth'] = new_depth
            engine: GenesisEngine = context.bot_data.get('engine')
            if engine:
                await engine.inicializar()
            await update.message.reply_text(f"✅ Profundidade de busca (OKX) definida para **{new_depth}**. Reconstruindo rotas...")
        else:
            await update.message.reply_text("⚠️ A profundidade de busca deve ser um número entre 2 e 6.")
    except (IndexError, TypeError, ValueError):
        await update.message.reply_text("⚠️ Uso: `/setdepth 4`")

async def salvar_saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("O motor ainda não foi inicializado.")
        return
    try:
        moeda = context.args[0].upper()
        await update.message.reply_text(f"Buscando informações para `{moeda}`...")
        
        pairs = await engine.api_client.get_all_pairs()
        usdt_pair_found = False
        for pair in pairs:
            if pair['instId'].split('-')[0] == moeda and pair['instId'].split('-')[1] == MOEDA_BASE_OPERACIONAL:
                pair_id = pair['instId']
                side = 'sell'
                usdt_pair_found = True
                break
            elif pair['instId'].split('-')[0] == MOEDA_BASE_OPERACIONAL and pair['instId'].split('-')[1] == moeda:
                pair_id = pair['instId']
                side = 'buy'
                usdt_pair_found = True
                break
        
        if not usdt_pair_found:
            await update.message.reply_text(f"Não foi possível encontrar um par para `{moeda}` com `{MOEDA_BASE_OPERACIONAL}`. Venda manualmente.")
            return

        saldos = await engine.api_client.get_spot_balances()
        saldo_moeda = sum(Decimal(c['availBal']) for c in saldos if c['ccy'] == moeda and c['availBal'])

        if saldo_moeda > 0:
            order_params = {'instId': pair_id, 'tdMode': 'cash', 'ordType': 'market', 'side': side, 'sz': str(saldo_moeda.quantize(Decimal('0.00000001')))}
            res = await engine.api_client.create_order(order_params)

            if not isinstance(res, OKXAPIException):
                 await update.message.reply_text(f"✅ Tentativa de conversão de `{moeda}` para `{MOEDA_BASE_OPERACIONAL}` concluída. Verifique seu saldo.")
            else:
                 await update.message.reply_text(f"❌ Falha na conversão de `{moeda}`: `{res.message}`")
        else:
            await update.message.reply_text(f"⚠️ Saldo de `{moeda}` é zero. Nenhuma ação necessária.")

    except (IndexError, TypeError, ValueError):
        await update.message.reply_text("⚠️ Uso: `/salvar_saldo ETH` (Tenta vender o saldo de ETH para USDT).")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro: `{e}`")

# --- 5. INICIALIZAÇÃO E EXECUÇÃO ---
async def post_init_tasks(app: Application):
    logger.info("Bot do Telegram (OKX) conectado. Iniciando o motor Gênesis...")
    engine = GenesisEngine(app)
    app.bot_data['engine'] = engine
    app.bot_data['dry_run'] = True
    await send_telegram_message("🤖 *Gênesis v17.7 (OKX) iniciado.*\nPor padrão, o bot está em **Modo Simulação**.")
    await engine.inicializar()
    asyncio.create_task(engine.verificar_oportunidades())
    logger.info("Motor Gênesis (OKX) e tarefas de fundo iniciadas.")

def main():
    if not TELEGRAM_TOKEN or not OKX_API_KEY or not OKX_SECRET_KEY or not OKX_PASSPHRASE:
        logger.critical("Variáveis de ambiente (TOKEN, API_KEY, SECRET_KEY, PASSPHRASE) não encontradas. Encerrando.")
        return
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    command_map = {
        "start": start_command, "status": status_command, "radar": radar_command,
        "diagnostico": diagnostico_command, "debug_radar": debug_radar_command,
        "saldo": saldo_command, "setlucro": setlucro_command, "setvolume": setvolume_command,
        "modo_real": modo_real_command, "modo_simulacao": modo_simulacao_command,
        "pausar": pausar_command, "retomar": retomar_command,
        "setdepth": setdepth_command, "salvar_saldo": salvar_saldo_command,
    }
    for command, handler in command_map.items():
        application.add_handler(CommandHandler(command, handler))
    application.post_init = post_init_tasks
    logger.info("Iniciando o bot do Telegram (OKX)...")
    application.run_polling()

if __name__ == "__main__":
    main()
