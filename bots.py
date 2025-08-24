# -*- coding: utf-8 -*-
# Gênesis v17.28 - "Estratégia Anti-Falha"
# Bot 1 (OKX) - v6.4: Conexão Primeiro. Estabelece a conexão com a OKX antes de iniciar o loop do Telegram.

import os
import asyncio
import logging
from decimal import Decimal, getcontext
import time
from datetime import datetime
import random

# === IMPORTAÇÃO CCXT E TELEGRAM ===
import ccxt.async_support as ccxt
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# ==============================================================================
# 1. CONFIGURAÇÃO GLOBAL E INICIALIZAÇÃO
# ==============================================================================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
getcontext().prec = 30

# Variáveis de Ambiente
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD")

# Parâmetros
TAXA_TAKER = Decimal("0.001")
MIN_PROFIT_DEFAULT = Decimal("0.4")
MARGEM_DE_SEGURANCA = Decimal("0.995")
MOEDA_BASE_OPERACIONAL = 'USDT'
MINIMO_ABSOLUTO_USDT = Decimal("3.1")
MIN_ROUTE_DEPTH = 3
MAX_ROUTE_DEPTH_DEFAULT = 3
FIAT_CURRENCIES = {'USD', 'EUR', 'GBP', 'JPY', 'BRL', 'AUD', 'CAD', 'CHF', 'CNY', 'HKD', 'SGD', 'KRW', 'INR', 'RUB', 'TRY', 'UAH', 'VND', 'THB', 'PHP', 'IDR', 'MYR', 'AED', 'SAR', 'ZAR', 'MXN', 'ARS', 'CLP', 'COP', 'PEN'}

# ==============================================================================
# 2. CLASSE DO MOTOR DE ARBITRAGEM (GenesisEngine)
# ==============================================================================
class GenesisEngine:
    # A classe agora recebe a exchange já inicializada
    def __init__(self, application: Application, exchange: ccxt.okx):
        self.app = application
        self.bot_data = application.bot_data
        self.exchange = exchange # Recebe a exchange pronta
        self.trade_lock = asyncio.Lock()
        
        self.bot_data.setdefault('is_running', True)
        self.bot_data.setdefault('min_profit', MIN_PROFIT_DEFAULT)
        self.bot_data.setdefault('dry_run', True)
        self.bot_data.setdefault('volume_percent', Decimal("100.0"))
        self.bot_data.setdefault('max_depth', MAX_ROUTE_DEPTH_DEFAULT)
        
        self.markets = {}
        self.graph = {}
        self.rotas_viaveis = []
        self.ecg_data = []
        self.stats = {'start_time': time.time(), 'ciclos_verificacao_total': 0, 'trades_executados': 0, 'lucro_total_sessao': Decimal('0'), 'erros_simulacao': 0, 'falhas_execucao': 0}
        self.bot_data['progress_status'] = "Iniciando..."

    # A inicialização da exchange foi movida para fora da classe
    async def carregar_mercados(self):
        try:
            self.markets = await self.exchange.load_markets()
            logger.info(f"Mercados da OKX carregados com sucesso na classe Engine. {len(self.markets)} mercados encontrados.")
        except Exception as e:
            logger.critical(f"❌ Falha ao carregar mercados na classe Engine: {e}", exc_info=True)
            await send_telegram_message(f"❌ Erro Crítico: Falha ao carregar mercados na Engine: `{e}`")
            raise e # Propaga o erro para parar a inicialização

    async def construir_rotas(self, max_depth: int):
        self.bot_data['progress_status'] = "Construindo mapa de rotas..."
        # ... (código de construir_rotas é idêntico ao anterior)
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
            if depth > max_depth: return
            for v in self.graph.get(u, []):
                if v == MOEDA_BASE_OPERACIONAL and len(path) >= MIN_ROUTE_DEPTH:
                    rota = path + [v]
                    if len(set(rota)) == len(rota) -1: todas_as_rotas.append(rota)
                elif v not in path: encontrar_ciclos_dfs(v, path + [v], depth + 1)
        encontrar_ciclos_dfs(MOEDA_BASE_OPERACIONAL, [MOEDA_BASE_OPERACIONAL], 1)
        self.rotas_viaveis = [tuple(rota) for rota in todas_as_rotas]
        random.shuffle(self.rotas_viaveis)
        await send_telegram_message(f"🗺️ Mapa de rotas reconstruído. {len(self.rotas_viaveis)} rotas serão monitoradas.")
        self.bot_data['progress_status'] = "Pronto para iniciar ciclos."

    # ... (O resto da classe GenesisEngine: _get_pair_details, verificar_oportunidades, _simular_trade, _executar_trade são idênticos à v6.2)
    def _get_pair_details(self, coin_from, coin_to):
        pair_buy = f"{coin_to}/{coin_from}"
        if pair_buy in self.markets: return pair_buy, 'buy'
        pair_sell = f"{coin_from}/{coin_to}"
        if pair_sell in self.markets: return pair_sell, 'sell'
        return None, None

    async def verificar_oportunidades(self):
        logger.info("Motor 'Conexão Primeiro' (v6.4) iniciado.")
        while True:
            await asyncio.sleep(1)
            if not self.bot_data.get('is_running', True):
                self.bot_data['progress_status'] = "Pausado."
                await asyncio.sleep(10)
                continue
            if self.trade_lock.locked():
                self.bot_data['progress_status'] = "Aguardando trava de segurança..."
                await asyncio.sleep(5)
                continue
            self.stats['ciclos_verificacao_total'] += 1
            logger.info(f"Iniciando ciclo de verificação #{self.stats['ciclos_verificacao_total']}...")
            try:
                balance = await self.exchange.fetch_balance()
                saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
                volume_a_usar = (saldo_disponivel * (self.bot_data['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA
                if volume_a_usar < MINIMO_ABSOLUTO_USDT:
                    self.bot_data['progress_status'] = f"Volume ({volume_a_usar:.2f} USDT) abaixo do mínimo."
                    await asyncio.sleep(20)
                    continue
                self.ecg_data = []
                total_rotas = len(self.rotas_viaveis)
                for i, cycle_tuple in enumerate(self.rotas_viaveis):
                    if not self.bot_data.get('is_running', True): break
                    self.bot_data['progress_status'] = f"Analisando... Rota {i+1}/{total_rotas}."
                    try:
                        resultado = await self._simular_trade(list(cycle_tuple), volume_a_usar)
                        if resultado: self.ecg_data.append(resultado)
                    except Exception as e:
                        self.stats['erros_simulacao'] += 1
                    if i % 100 == 0: await asyncio.sleep(0.1)
                if self.ecg_data:
                    self.ecg_data.sort(key=lambda x: x['profit'], reverse=True)
                    melhor_rota = self.ecg_data[0]
                    if melhor_rota['profit'] > self.bot_data['min_profit']:
                        await self._executar_trade(melhor_rota['cycle'], volume_a_usar)
                self.bot_data['progress_status'] = f"Ciclo #{self.stats['ciclos_verificacao_total']} concluído. Aguardando 10s..."
                await asyncio.sleep(10)
            except Exception as e:
                logger.error(f"Erro CRÍTICO no loop de verificação: {e}", exc_info=True)
                await send_telegram_message(f"⚠️ **Erro Grave no Bot:** `{type(e).__name__}`. Verifique os logs.")
                self.bot_data['progress_status'] = f"Erro crítico. Reiniciando em 60s."
                await asyncio.sleep(60)

    async def _simular_trade(self, cycle_path, volume_inicial):
        current_amount = volume_inicial
        for i in range(len(cycle_path) - 1):
            coin_from, coin_to = cycle_path[i], cycle_path[i+1]
            pair_id, side = self._get_pair_details(coin_from, coin_to)
            if not pair_id: return None
            orderbook = await self.exchange.fetch_order_book(pair_id)
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
                else:
                    if remaining_amount <= size:
                        final_traded_amount += remaining_amount * price
                        remaining_amount = Decimal('0'); break
                    else:
                        final_traded_amount += size * price
                        remaining_amount -= size
            if remaining_amount > 0: return None
            current_amount = final_traded_amount * (1 - TAXA_TAKER)
        lucro_percentual = ((current_amount - volume_inicial) / volume_inicial) * 100
        return {'cycle': cycle_path, 'profit': lucro_percentual}

    async def _executar_trade(self, cycle_path, volume_a_usar):
        await self.trade_lock.acquire()
        try:
            logger.info(f"🚀 Oportunidade encontrada. Executando rota: {' -> '.join(cycle_path)}.")
            if self.bot_data['dry_run']:
                lucro_simulado = self.ecg_data[0]['profit']
                await send_telegram_message(f"✅ **Simulação:** Oportunidade encontrada. Lucro: `{lucro_simulado:.4f}%`.")
                self.stats['trades_executados'] += 1
                return
            moedas_presas = []
            current_amount_asset = volume_a_usar
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                if not pair_id: raise Exception(f"Par inválido: {coin_from}/{coin_to}")
                market = self.exchange.market(pair_id)
                try:
                    if side == 'buy':
                        order = await self.exchange.create_market_order_with_cost(symbol=pair_id, side=side, cost=current_amount_asset)
                    else:
                        amount_to_trade = self.exchange.amount_to_precision(pair_id, current_amount_asset)
                        min_amount = Decimal(str(market['limits']['amount']['min']))
                        if Decimal(amount_to_trade) < min_amount: raise ValueError(f"Quantidade ({amount_to_trade} {market['base']}) abaixo do mínimo.")
                        order = await self.exchange.create_market_order(symbol=pair_id, side=side, amount=amount_to_trade)
                    await asyncio.sleep(1.5)
                    order_status = await self.exchange.fetch_order(order['id'], pair_id)
                    if order_status['status'] != 'closed': raise Exception(f"Ordem {order['id']} não preenchida a tempo.")
                    filled_amount = Decimal(str(order_status['filled']))
                    if side == 'buy':
                        current_amount_asset = filled_amount * (1 - TAXA_TAKER)
                        moedas_presas.append({'symbol': coin_to, 'amount': current_amount_asset})
                    else:
                        filled_price = Decimal(str(order_status['average']))
                        current_amount_asset = (filled_amount * filled_price) * (1 - TAXA_TAKER)
                        moedas_presas.pop()
                except Exception as leg_error:
                    self.stats['falhas_execucao'] += 1
                    await send_telegram_message(f"🔴 **FALHA NA PERNA {i+1}!**\n`{' -> '.join(cycle_path)}`\n**Erro:** `{leg_error}`")
                    if moedas_presas:
                        ativo_preso = moedas_presas[-1]
                        await send_telegram_message(f"⚠️ **CAPITAL PRESO!**\nAtivo: `{ativo_preso['amount']:.4f} {ativo_preso['symbol']}`.\nIniciando venda de emergência...")
                        try:
                            reversal_pair, _ = self._get_pair_details(ativo_preso['symbol'], 'USDT')
                            if reversal_pair:
                                reversal_amount = self.exchange.amount_to_precision(reversal_pair, ativo_preso['amount'])
                                await self.exchange.create_market_sell_order(symbol=reversal_pair, amount=reversal_amount)
                                await send_telegram_message("✅ Venda de Emergência Executada!")
                            else:
                                await send_telegram_message("❌ Falha na Venda de Emergência: Par com USDT não encontrado.")
                        except Exception as reversal_error:
                            await send_telegram_message(f"❌ FALHA CRÍTICA NA VENDA DE EMERGÊNCIA: `{reversal_error}`. VERIFIQUE A CONTA!")
                    return
            final_amount = current_amount_asset
            lucro_real_percent = ((final_amount - volume_a_usar) / volume_a_usar) * 100
            lucro_real_usdt = final_amount - volume_a_usar
            self.stats['trades_executados'] += 1
            self.stats['lucro_total_sessao'] += lucro_real_usdt
            await send_telegram_message(f"✅ **Arbitragem Concluída!**\nRota: `{' -> '.join(cycle_path)}`\nLucro: `{lucro_real_usdt:.4f} USDT` (`{lucro_real_percent:.4f}%`)")
        finally:
            self.trade_lock.release()

# ==============================================================================
# 3. FUNÇÕES E COMANDOS DO TELEGRAM
# ==============================================================================
# As funções de comando (start, status, etc.) são idênticas às da v6.2
# A única diferença é que agora elas usam context.bot_data['engine'] que foi criado no post_init
async def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem no Telegram: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("👋 Olá! Sou o Gênesis v6.4 'Conexão Primeiro'. Use /ajuda.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    engine = context.bot_data.get('engine')
    if not engine: await update.message.reply_text("Motor não inicializado."); return
    dry_run = engine.bot_data.get('dry_run', True)
    status_text = "Em operação" if engine.bot_data.get('is_running', True) else "Pausado"
    dry_run_text = "Simulação" if dry_run else "Modo Real"
    response = (f"🤖 **Status do Gênesis v6.4:**\n"
                f"**Status:** `{status_text}`\n"
                f"**Modo:** `{dry_run_text}`\n"
                f"**Lucro Mínimo:** `{engine.bot_data.get('min_profit'):.4f}%`\n"
                f"**Volume de Trade:** `{engine.bot_data.get('volume_percent'):.2f}%` do saldo\n"
                f"**Profundidade de Rotas:** `{engine.bot_data.get('max_depth')}`\n\n"
                f"**Progresso:** `{engine.bot_data.get('progress_status')}`")
    await update.message.reply_text(response, parse_mode="Markdown")

# ... (todos os outros comandos: saldo, modo_real, etc. são idênticos)
async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine = context.bot_data.get('engine')
    if not engine or not engine.exchange: await update.message.reply_text("Engine não inicializada."); return
    try:
        balance = await engine.exchange.fetch_balance()
        saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
        await update.message.reply_text(f"📊 Saldo OKX: `{saldo_disponivel:.4f} {MOEDA_BASE_OPERACIONAL}`", parse_mode="Markdown")
    except Exception as e: await update.message.reply_text(f"❌ Erro ao buscar saldo: {e}")

async def modo_real_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['engine'].bot_data['dry_run'] = False
    await update.message.reply_text("✅ **Modo Real Ativado!**")

async def modo_simulacao_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['engine'].bot_data['dry_run'] = True
    await update.message.reply_text("✅ **Modo Simulação Ativado!**")

async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.bot_data['engine'].bot_data['min_profit'] = Decimal(context.args[0])
        await update.message.reply_text(f"✅ Lucro mínimo definido para `{context.bot_data['engine'].bot_data['min_profit']:.4f}%`.")
    except: await update.message.reply_text("❌ Uso: /setlucro <porcentagem>")

async def setvolume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        volume = Decimal(context.args[0])
        if not (0 < volume <= 100): raise ValueError
        context.bot_data['engine'].bot_data['volume_percent'] = volume
        await update.message.reply_text(f"✅ Volume de trade definido para `{volume:.2f}%`.")
    except: await update.message.reply_text("❌ Uso: /setvolume <porcentagem entre 1-100>")

async def pausar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['engine'].bot_data['is_running'] = False
    await update.message.reply_text("⏸️ Motor pausado.")

async def retomar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['engine'].bot_data['is_running'] = True
    await update.message.reply_text("▶️ Motor retomado.")

async def setdepth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine = context.bot_data.get('engine')
    if not engine: return
    try:
        depth = int(context.args[0])
        if not (MIN_ROUTE_DEPTH <= depth <= 5): raise ValueError
        engine.bot_data['max_depth'] = depth
        await engine.construir_rotas(depth)
        await update.message.reply_text(f"✅ Profundidade de rotas definida para `{depth}`.")
    except: await update.message.reply_text(f"❌ Uso: /setdepth <número de {MIN_ROUTE_DEPTH} a 5>")

async def ajuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 **Comandos v6.4:**\n"
        "`/status`, `/saldo`, `/modo_real`, `/modo_simulacao`, `/setlucro`, `/setvolume`, `/pausar`, `/retomar`, `/setdepth`"
    )

# ==============================================================================
# 4. FUNÇÃO PRINCIPAL DE INICIALIZAÇÃO (v6.4)
# ==============================================================================
async def post_init(application: Application) -> None:
    """Função que roda após a inicialização do bot para iniciar o motor."""
    logger.info("Bot do Telegram inicializado. Iniciando tarefas em segundo plano...")
    engine = application.bot_data['engine']
    
    # O motor já tem a exchange, agora só precisa carregar os mercados e construir as rotas
    await engine.carregar_mercados()
    await engine.construir_rotas(engine.bot_data['max_depth'])
    
    # Cria a tarefa do motor para rodar em segundo plano
    asyncio.create_task(engine.verificar_oportunidades())

async def main() -> None:
    """Conecta na exchange PRIMEIRO, depois inicia o bot."""
    if not all([TELEGRAM_TOKEN, OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSWORD]):
        logger.critical("Variáveis de ambiente não configuradas. Verifique TELEGRAM_TOKEN e as chaves da OKX.")
        return

    # --- ETAPA 1: CONECTAR NA EXCHANGE ---
    logger.info("ETAPA 1: Tentando conectar na OKX ANTES de iniciar o Telegram...")
    exchange = None
    try:
        exchange = ccxt.okx({
            'apiKey': OKX_API_KEY,
            'secret': OKX_API_SECRET,
            'password': OKX_API_PASSWORD,
            'options': {'defaultType': 'spot'},
        })
        # Testa a conexão buscando o saldo (uma chamada autenticada)
        await exchange.fetch_balance()
        logger.info("✅ SUCESSO: Conexão com a OKX estabelecida com sucesso.")
    except Exception as e:
        logger.critical(f"❌ FALHA CRÍTICA ao conectar na OKX: {e}", exc_info=True)
        # Envia uma mensagem de falha antes de morrer (se possível)
        await send_telegram_message(f"❌ **FALHA CRÍTICA NA INICIALIZAÇÃO**\nNão foi possível conectar à OKX.\n**Erro:** `{e}`\nO bot não será iniciado.")
        if exchange:
            await exchange.close()
        return # Impede o bot de iniciar

    # --- ETAPA 2: INICIAR O BOT DO TELEGRAM ---
    logger.info("ETAPA 2: Iniciando o bot do Telegram...")
    
    application = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )
    
    # Cria a instância do motor e a anexa ao bot para ser usada no post_init
    engine = GenesisEngine(application, exchange)
    application.bot_data['engine'] = engine

    # Adiciona os handlers de comando
    command_map = {
        "start": start_command, "status": status_command, "saldo": saldo_command,
        "modo_real": modo_real_command, "modo_simulacao": modo_simulacao_command,
        "setlucro": setlucro_command, "setvolume": setvolume_command,
        "pausar": pausar_command, "retomar": retomar_command,
        "setdepth": setdepth_command, "ajuda": ajuda_command,
    }
    for command, handler in command_map.items():
        application.add_handler(CommandHandler(command, handler))
    
    # Inicia o bot
    logger.info("Iniciando polling do Telegram...")
    # O run_polling é bloqueante, então a sessão da exchange será fechada quando o bot parar
    try:
        await application.run_polling()
    finally:
        logger.info("Fechando a sessão da exchange ao encerrar o bot.")
        await exchange.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot desligado manualmente.")

