# -*- coding: utf-8 -*-
# Gênesis v17.7 - Versão com Simulação de Alta Fidelidade
# Melhorias:
# 1. Aumentada a profundidade do Order Book para uma análise de liquidez mais completa.
# 2. Implementada a lógica de precisão de quantidade (amount_precision) na simulação,
#    refletindo as regras reais da exchange e evitando perdas por arredondamento.

# Dependências (requirements.txt):
# okx-python-sdk-async
# python-telegram-bot
# aiohttp

import os
import asyncio
import logging
from decimal import Decimal, getcontext, ROUND_DOWN
import time
import uuid
from datetime import datetime, timezone

from okx.exceptions import OkxAPIException
import okx.Account_api as Account
import okx.Trade_api as Trade
import okx.Public_api as Public

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# --- 1. CONFIGURAÇÕES GLOBAIS ---
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_SUREBET")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")
OKX_TEST_MODE = os.getenv("OKX_TEST_MODE", "True") == "True"

# --- Pilares da Estratégia v17.7 ---
TAXA_OPERACAO = Decimal("0.002")
MIN_PROFIT_DEFAULT = Decimal("0.01")
MARGEM_DE_SEGURANCA = Decimal("0.995")
MOEDA_BASE_OPERACIONAL = 'USDT'
MINIMO_ABSOLUTO_USDT = Decimal("3.1")
MAX_ROUTE_DEPTH = 4
ORDER_BOOK_DEPTH = 100

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
getcontext().prec = 30

# --- 2. OKX API CLIENT ---
class OKXApiClient:
    def __init__(self, api_key, secret_key, passphrase, test_mode):
        self.flag = '0' if not test_mode else '1'
        self.tradeAPI = Trade.TradeAPI(api_key, secret_key, passphrase, False, self.flag)
        self.accountAPI = Account.AccountAPI(api_key, secret_key, passphrase, False, self.flag)
        self.publicAPI = Public.PublicAPI(api_key, secret_key, passphrase, False, self.flag)

    async def _execute_api_call(self, api_call, *args, **kwargs):
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: api_call(*args, **kwargs))
        except OkxAPIException as ex:
            return ex
        except Exception as e:
            logger.error(f"Erro na chamada da API: {e}")
            return None

    async def get_all_pairs(self):
        return await self._execute_api_call(self.publicAPI.get_instruments, instType='SPOT')

    async def get_spot_balances(self):
        return await self._execute_api_call(self.accountAPI.get_balances, ccy=MOEDA_BASE_OPERACIONAL)

    async def create_order(self, instId, side, sz):
        return await self._execute_api_call(self.tradeAPI.place_order, instId=instId, tdMode='cash', side=side, ordType='market', sz=sz)

    async def get_order_book(self, instId):
        return await self._execute_api_call(self.publicAPI.get_orderbook, instId=instId, sz=ORDER_BOOK_DEPTH)

    async def get_single_pair(self, instId):
        return await self._execute_api_call(self.publicAPI.get_instruments, instType='SPOT', instId=instId)

# --- 3. GÊNESIS ENGINE v17.7 ---
class GenesisEngine:
    def __init__(self, application: Application):
        self.app = application
        self.bot_data = application.bot_data
        self.api_client = OKXApiClient(OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, OKX_TEST_MODE)
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
        logger.info("Gênesis v17.7 (Simulação de Alta Fidelidade): Iniciando...")
        all_pairs_data = await self.api_client.get_all_pairs()
        if not all_pairs_data or isinstance(all_pairs_data, OkxAPIException):
            logger.critical("Gênesis: Não foi possível obter os pares da OKX. Verifique as chaves da API e a conexão."); return

        for pair_data in all_pairs_data['data']:
            if pair_data['state'] == 'live':
                base, quote = pair_data['baseCcy'], pair_data['quoteCcy']
                self.pair_rules[pair_data['instId']] = {
                    'base': base,
                    'quote': quote,
                    'min_sz': Decimal(pair_data['minSz']),
                    'sz_prec': int(pair_data['szCcy']),
                    'px_prec': int(pair_data['tickSz'])
                }
                if base not in self.graph:
                    self.graph[base] = []
                if quote not in self.graph:
                    self.graph[quote] = []
                self.graph[base].append(quote)
                self.graph[quote].append(base)

        logger.info(f"Gênesis: Mapa construído. Buscando rotas de até {self.bot_data['max_route_depth']} passos...")
        start_node = MOEDA_BASE_OPERACIONAL

        def encontrar_ciclos_dfs(u, path, depth):
            if depth > self.bot_data['max_route_depth']:
                return
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
        if pair_v1 in self.pair_rules:
            return pair_v1, 'sell'
        pair_v2 = f"{coin_to}-{coin_from}"
        if pair_v2 in self.pair_rules:
            return pair_v2, 'buy'
        return None, None

    def _simular_realidade_com_cache(self, cycle_path, valor_inicial_usdt, order_books_cache):
        try:
            valor_simulado = valor_inicial_usdt
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                
                if not pair_id or pair_id not in order_books_cache:
                    return None

                pair_info = self.pair_rules.get(pair_id)
                if not pair_info:
                    return None

                amount_precision = pair_info['sz_prec']
                quantizer = Decimal('1e-' + str(amount_precision))

                order_book = order_books_cache[pair_id]

                if side == 'buy':
                    valor_a_gastar = valor_simulado
                    quantidade_comprada = Decimal('0')
                    for preco_str, quantidade_str in order_book['asks']:
                        preco, quantidade_disponivel = Decimal(preco_str), Decimal(quantidade_str)
                        custo_nivel = preco * quantidade_disponivel
                        if valor_a_gastar > custo_nivel:
                            quantidade_comprada += quantidade_disponivel
                            valor_a_gastar -= custo_nivel
                        else:
                            if preco == 0:
                                return None
                            qtd_a_comprar = valor_a_gastar / preco
                            qtd_a_comprar_arredondada = qtd_a_comprar.quantize(quantizer, rounding=ROUND_DOWN)
                            if qtd_a_comprar_arredondada <= 0:
                                break
                            quantidade_comprada += qtd_a_comprar_arredondada
                            valor_a_gastar = Decimal('0')
                            break
                    if valor_a_gastar > 0:
                        return None
                    valor_simulado = quantidade_comprada
                else:  # side == 'sell'
                    quantidade_a_vender = valor_simulado.quantize(quantizer, rounding=ROUND_DOWN)
                    if quantidade_a_vender <= 0:
                        return None
                    valor_recebido = Decimal('0')
                    for preco_str, quantidade_str in order_book['bids']:
                        preco, quantidade_disponivel = Decimal(preco_str), Decimal(quantidade_str)
                        if quantidade_a_vender > quantidade_disponivel:
                            valor_recebido += quantidade_disponivel * preco
                            quantidade_a_vender -= quantidade_disponivel
                        else:
                            valor_recebido += quantidade_a_vender * preco
                            quantidade_a_vender = Decimal('0')
                            break
                    if quantidade_a_vender > 0:
                        return None
                    valor_simulado = valor_recebido

                valor_simulado *= (1 - TAXA_OPERACAO)

            lucro_bruto = valor_simulado - valor_inicial_usdt
            if valor_inicial_usdt == 0:
                return Decimal('0')
            lucro_percentual = (lucro_bruto / valor_inicial_usdt) * 100
            return lucro_percentual
        except Exception as e:
            logger.error(f"Erro na simulação para a rota {' -> '.join(cycle_path)}: {e}", exc_info=True)
            return None


    async def verificar_oportunidades(self):
        logger.info("Gênesis: Motor 'O Caçador de Migalhas' (OKX) iniciado.")
        while True:
            if not self.bot_data.get('is_running', True) or self.trade_lock.locked():
                await asyncio.sleep(1)
                continue
            try:
                self.stats['ciclos_verificacao_total'] += 1
                self.stats['ultimo_ciclo_timestamp'] = time.time()
                
                saldos = await self.api_client.get_spot_balances()
                if not saldos or isinstance(saldos, OkxAPIException) or 'data' not in saldos or not saldos['data']:
                    await asyncio.sleep(5)
                    continue
                
                saldo_disponivel = Decimal(saldos['data'][0]['availBal'])
                volume_a_simular = saldo_disponivel * (self.bot_data['volume_percent'] / 100) * MARGEM_DE_SEGURANCA
                
                if volume_a_simular < MINIMO_ABSOLUTO_USDT:
                    await asyncio.sleep(10)
                    continue

                pares_necessarios = set()
                for rota in self.rotas_monitoradas:
                    for i in range(len(rota) - 1):
                        par, _ = self._get_pair_details(rota[i], rota[i+1])
                        if par:
                            pares_necessarios.add(par)

                tasks = [self.api_client.get_order_book(par) for par in pares_necessarios]
                results = await asyncio.gather(*tasks)

                order_books_cache = {}
                for par, book in zip(pares_necessarios, results):
                    if book and 'data' in book and book['data']:
                        order_books_cache[par] = book['data'][0]

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
        investimento_inicial_usdt = volume_a_usar
        try:
            if is_dry_run:
                await send_telegram_message(f"🎯 **Alvo Realista na Mira (Simulação)**\n"
                                            f"Rota: `{' -> '.join(cycle_path)}`\n"
                                            f"Lucro Líquido Realista: `{self.simulacao_data[0]['profit']:.4f}%`")
                return

            await send_telegram_message(f"🚀 **Iniciando Trade REAL...**\n"
                                        f"Rota: `{' -> '.join(cycle_path)}`\n"
                                        f"Investimento Planejado: `{investimento_inicial_usdt:.4f} {cycle_path[0]}`")

            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)

                saldos_pre_trade = await self.api_client.get_spot_balances()
                saldo_a_negociar = Decimal(saldos_pre_trade['data'][0]['availBal'])

                if saldo_a_negociar <= 0:
                    await send_telegram_message(f"❌ **FALHA CRÍTICA (Passo {i+1})**\n"
                                                f"Saldo de `{coin_from}` é zero. Abortando.")
                    return

                # --- Lógica de Correção do Bug ---
                pair_info = self.pair_rules.get(pair_id)
                if side == 'buy':
                    order_book = await self.api_client.get_order_book(pair_id)
                    if not order_book or 'data' not in order_book or not order_book['data'][0]['asks']:
                        await send_telegram_message(f"❌ **FALHA CRÍTICA (Passo {i+1})**\n"
                                                    f"Não foi possível obter o book de ordens para `{pair_id}`. Abortando.")
                        return
                    
                    melhor_preco_ask = Decimal(order_book['data'][0]['asks'][0][0])
                    if melhor_preco_ask == 0:
                        await send_telegram_message(f"❌ **FALHA CRÍTICA (Passo {i+1})**\n"
                                                    f"Preço de compra (`{pair_id}`) é zero. Abortando.")
                        return

                    # Calcula a quantidade da moeda-base a ser comprada
                    sz = (saldo_a_negociar / melhor_preco_ask).quantize(Decimal('1e-' + str(pair_info['sz_prec'])), rounding=ROUND_DOWN)

                else: # side == 'sell'
                    # Já temos o saldo da moeda-base, é só usar
                    sz = saldo_a_negociar.quantize(Decimal('1e-' + str(pair_info['sz_prec'])), rounding=ROUND_DOWN)
                
                if sz < pair_info['min_sz']:
                    await send_telegram_message(f"❌ **FALHA CRÍTICA (Passo {i+1})**\n"
                                                f"Tamanho da ordem (`{sz}`) é menor que o mínimo permitido (`{pair_info['min_sz']}`). Abortando.")
                    return
                # --- Fim da Lógica de Correção ---

                await send_telegram_message(f"⏳ Passo {i+1}/{len(cycle_path)-1}: Negociando `{sz} {pair_info['base']}` para `{pair_info['quote']}`.")

                res = await self.api_client.create_order(instId=pair_id, side=side, sz=str(sz))

                if not res or isinstance(res, OkxAPIException) or 'data' not in res or not res['data'] or res['data'][0]['sCode'] != '0':
                    error_msg = res.sMsg if isinstance(res, OkxAPIException) else "Falha na criação da ordem."
                    await send_telegram_message(f"❌ **FALHA NO PASSO {i+1} ({pair_id})**\n**Motivo:** `{error_msg}`\n**ALERTA:** Saldo em `{coin_from}` pode estar preso!")
                    return

                await asyncio.sleep(2)

            saldos_pos_trade = await self.api_client.get_spot_balances()
            resultado_final = Decimal(saldos_pos_trade['data'][0]['availBal'])
            lucro_real = resultado_final - investimento_inicial_usdt

            resultado_emoji = "✅" if lucro_real > 0 else "🔻"
            await send_telegram_message(f"{resultado_emoji} **Trade Concluído (OKX)!**\n"
                                        f"Rota: `{' -> '.join(cycle_path)}`\n"
                                        f"Investimento: `{investimento_inicial_usdt:.4f} {cycle_path[0]}`\n"
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
    if not TELEGRAM_TOKEN or not ADMIN_CHAT_ID:
        return
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem no Telegram: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Olá! Gênesis v17.7 (Simulação de Alta Fidelidade) online. Use /status para começar.")

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
        await update.message.reply_text("📡 Radar do Caçador (OKX): Nenhuma simulação foi concluída ainda.")
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
        await update.message.reply_text("🔎 Nenhuma simulação foi executada ainda.")
        return

    all_results = sorted(engine.simulacao_data, key=lambda x: x['profit'], reverse=True)
    msg = "🐛 **Radar de Depuração (Todas as Rotas Simuladas)**\n\n"

    for i, result in enumerate(all_results[:10]):
        lucro = result['profit']
        emoji = "🔼" if lucro >= 0 else "🔽"
        rota_fmt = ' -> '.join(result['cycle'])
        msg += f"**{i+1}. Rota:** `{rota_fmt}`\n"
        msg += f"  **Lucro Líquido Realista:** `{emoji} {lucro:.4f}%`\n\n"

    msg += "_(Exibindo as 10 melhores/piores.)_"
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

    msg = (f"**🩺 Diagnóstico Interno - Gênesis v17.7**\n\n"
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
        saldos_data = await engine.api_client.get_spot_balances()
        if not saldos_data or isinstance(saldos_data, OkxAPIException) or 'data' not in saldos_data or not saldos_data['data']:
            await update.message.reply_text(f"❌ Erro ao buscar saldos: {saldos_data.sMsg if isinstance(saldos_data, OkxAPIException) else 'Resposta vazia'}")
            return
        
        account_balances = saldos_data['data'][0]['details'] if saldos_data and 'data' in saldos_data and saldos_data['data'] else []
        if not account_balances:
            await update.message.reply_text("Nenhum saldo encontrado.")
            return

        msg = "**💰 Saldos Atuais (Spot OKX)**\n\n"
        for conta in account_balances:
            if Decimal(conta['availBal']) > 0:
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

        pair_id, side = None, None
        for p_id, p_rules in engine.pair_rules.items():
            if p_rules['base'] == moeda and p_rules['quote'] == MOEDA_BASE_OPERACIONAL:
                pair_id, side = p_id, 'sell'
                break
            elif p_rules['base'] == MOEDA_BASE_OPERACIONAL and p_rules['quote'] == moeda:
                pair_id, side = p_id, 'buy'
                break

        if not pair_id:
            await update.message.reply_text(f"Não foi possível encontrar um par para `{moeda}` com `{MOEDA_BASE_OPERACIONAL}`.")
            return

        saldos_data = await engine.api_client.get_spot_balances()
        account_balances = saldos_data['data'][0]['details'] if saldos_data and 'data' in saldos_data and saldos_data['data'] else []
        saldo_moeda = Decimal('0')
        for conta in account_balances:
            if conta['ccy'] == moeda:
                saldo_moeda = Decimal(conta['availBal'])
                break
        
        if saldo_moeda > 0:
            pair_info = engine.pair_rules.get(pair_id)
            quantizer = Decimal('1e-' + str(pair_info['sz_prec']))
            amount_to_trade = saldo_moeda.quantize(quantizer, rounding=ROUND_DOWN)

            if amount_to_trade < pair_info['min_sz']:
                await update.message.reply_text(f"⚠️ Saldo de `{moeda}` (`{saldo_moeda}`) é muito pequeno para ser negociado. Mínimo: `{pair_info['min_sz']}`.")
                return

            res = await engine.api_client.create_order(instId=pair_id, side=side, sz=str(amount_to_trade))

            if not isinstance(res, OkxAPIException) and res and 'data' in res and res['data'] and res['data'][0]['sCode'] == '0':
                 await update.message.reply_text(f"✅ Tentativa de conversão de `{moeda}` para `{MOEDA_BASE_OPERACIONAL}` concluída. Verifique seu saldo.")
            else:
                 error_msg = res.sMsg if isinstance(res, OkxAPIException) else "Falha na conversão."
                 await update.message.reply_text(f"❌ Falha na conversão de `{moeda}`: `{error_msg}`")
        else:
            await update.message.reply_text(f"⚠️ Saldo de `{moeda}` é zero. Nenhuma ação necessária.")

    except (IndexError, TypeError, ValueError):
        await update.message.reply_text("⚠️ Uso: `/salvar_saldo ETH` (Tenta vender o saldo de ETH para USDT).")


async def post_init_tasks(app: Application):
    logger.info("Iniciando motor Gênesis v17.7 (OKX)...")
    if not all([OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, TELEGRAM_TOKEN, ADMIN_CHAT_ID]):
        logger.critical("❌ Falha crítica: Variáveis de ambiente incompletas.")
        return
    engine = GenesisEngine(app)
    app.bot_data['engine'] = engine
    try:
        await send_telegram_message("🤖 Gênesis v17.7 (OKX) iniciado. Carregando dados...")
        await engine.inicializar()
        asyncio.create_task(engine.verificar_oportunidades())
        logger.info("Motor e tarefas de fundo iniciadas.")
    except Exception as e:
        logger.error(f"❌ Erro ao inicializar o motor: {e}", exc_info=True)
        await send_telegram_message("❌ ERRO CRÍTICO: Não foi possível inicializar o bot. Verifique os logs.")

def main():
    if not TELEGRAM_TOKEN:
        logger.critical("Token do Telegram não encontrado.")
        return
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    command_map = {
        "start": start_command,
        "status": status_command,
        "radar": radar_command,
        "debug_radar": debug_radar_command,
        "diagnostico": diagnostico_command,
        "saldo": saldo_command,
        "modo_real": modo_real_command,
        "modo_simulacao": modo_simulacao_command,
        "setlucro": setlucro_command,
        "setvolume": setvolume_command,
        "pausar": pausar_command,
        "retomar": retomar_command,
        "setdepth": setdepth_command,
        "salvar_saldo": salvar_saldo_command,
    }
    for command, handler in command_map.items():
        application.add_handler(CommandHandler(command, handler))

    application.post_init = post_init_tasks
    logger.info("Iniciando bot do Telegram...")
    application.run_polling()

if __name__ == "__main__":
    main()
