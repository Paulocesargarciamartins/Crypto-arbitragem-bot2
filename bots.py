# -*- coding: utf-8 -*-
# Gênesis v17.12 - "Ataque aos Erros de Preço"
# Bot 1 (OKX) - CORREÇÃO FINAL: Ajuste na inicialização da CCXT conforme documentação.

import os
import asyncio
import logging
from decimal import Decimal, getcontext
import time
from datetime import datetime
import json

# === IMPORTAÇÃO CCXT E TELEGRAM ===
import ccxt.async_support as ccxt
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ==============================================================================
# 1. CONFIGURAÇÃO GLOBAL E INICIALIZAÇÃO
# ==============================================================================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
getcontext().prec = 30

# O código lê as variáveis do ambiente Heroku (Config Vars)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD") # Esta é a Passphrase

# ... (o resto das suas configurações globais permanece igual)
TAXA_TAKER = Decimal("0.001")
MIN_PROFIT_DEFAULT = Decimal("0.05")
MARGEM_DE_SEGURANCA = Decimal("0.995")
MOEDA_BASE_OPERACIONAL = 'USDT'
MINIMO_ABSOLUTO_USDT = Decimal("3.1")
MIN_ROUTE_DEPTH = 3
MAX_ROUTE_DEPTH_DEFAULT = 3
MARGEM_PRECO_TAKER = Decimal("1.0001")
FIAT_CURRENCIES = {'BRL', 'USD', 'EUR', 'JPY', 'GBP', 'AUD', 'CAD', 'CHF', 'CNY'}

# ==============================================================================
# 2. CLASSE DO MOTOR DE ARBITRAGEM (GenesisEngine)
# ==============================================================================
class GenesisEngine:
    def __init__(self, application: Application):
        self.app = application
        self.bot_data = application.bot_data
        self.exchange = None
        # ... (o resto do seu __init__ permanece igual)
        self.bot_data.setdefault('is_running', True)
        self.bot_data.setdefault('min_profit', MIN_PROFIT_DEFAULT)
        self.bot_data.setdefault('dry_run', True)
        self.bot_data.setdefault('volume_percent', Decimal("100.0"))
        self.bot_data.setdefault('max_depth', MAX_ROUTE_DEPTH_DEFAULT)
        self.bot_data.setdefault('stop_loss_usdt', None)
        self.markets = {}
        self.graph = {}
        self.rotas_viaveis = []
        self.ecg_data = []
        self.current_cycle_results = []
        self.trade_lock = asyncio.Lock()
        self.bot_data.setdefault('daily_profit_usdt', Decimal('0'))
        self.bot_data.setdefault('last_reset_day', datetime.utcnow().day)
        self.stats = {'start_time': time.time(), 'ciclos_verificacao_total': 0, 'trades_executados': 0, 'lucro_total_sessao': Decimal('0'), 'erros_simulacao': 0}
        self.bot_data['progress_status'] = "Iniciando..."

    async def inicializar_exchange(self):
        if not all([OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSWORD]):
            await send_telegram_message("❌ Falha crítica: Verifique as chaves da API da OKX na Heroku.")
            return False
        try:
            # ==================================================================
            # === CORREÇÃO APLICADA AQUI ===
            # A estrutura de inicialização foi ajustada para ser mais robusta
            # e compatível com a forma como a OKX espera os dados,
            # especialmente a 'password' (passphrase).
            # ==================================================================
            exchange_config = {
                'apiKey': OKX_API_KEY,
                'secret': OKX_API_SECRET,
                'password': OKX_API_PASSWORD,
                'options': {
                    'defaultType': 'spot',
                },
            }
            self.exchange = ccxt.okx(exchange_config)
            # ==================================================================

            self.markets = await self.exchange.load_markets()
            logger.info(f"Conectado à OKX. {len(self.markets)} mercados carregados.")
            return True
        except Exception as e:
            # Este bloco de erro agora nos dará uma mensagem mais específica da CCXT
            logger.critical(f"❌ Falha ao conectar com a OKX: {e}", exc_info=True)
            await send_telegram_message(f"❌ Erro de Conexão com a OKX: `{type(e).__name__}: {e}`.")
            if self.exchange: await self.exchange.close()
            return False

    # ... (O RESTO DO SEU CÓDIGO CONTINUA EXATAMENTE IGUAL AQUI)
    # Copie e cole o resto do seu arquivo bots.py, da função "construir_rotas"
    # em diante, pois ele está correto.
    async def construir_rotas(self, max_depth: int):
        self.bot_data['progress_status'] = "Construindo mapa de rotas..."
        logger.info(f"Construindo mapa (Profundidade: {max_depth})...")
        self.graph = {}
        active_markets = {s: m for s, m in self.markets.items() if m.get('active') and m.get('base') and m.get('quote') and m['base'] not in FIAT_CURRENCIES and m['quote'] not in FIAT_CURRENCIES}
        for symbol, market in active_markets.items():
            base, quote = market['base'], market['quote']
            if base not in self.graph: self.graph[base] = []
            if quote not in self.graph: self.graph[quote] = []
            self.graph[base].append(quote)
            self.graph[quote].append(base)
        logger.info(f"Mapa construído com {len(self.graph)} nós. Buscando rotas...")
        todas_as_rotas = []
        def encontrar_ciclos_dfs(u, path, depth):
            if depth > max_depth: return
            for v in self.graph.get(u, []):
                if v == MOEDA_BASE_OPERACIONAL and len(path) >= MIN_ROUTE_DEPTH:
                    rota = path + [v]
                    if len(set(rota)) == len(rota) -1:
                         todas_as_rotas.append(rota)
                elif v not in path:
                    encontrar_ciclos_dfs(v, path + [v], depth + 1)
        encontrar_ciclos_dfs(MOEDA_BASE_OPERACIONAL, [MOEDA_BASE_OPERACIONAL], 1)
        self.rotas_viaveis = [tuple(rota) for rota in todas_as_rotas]
        self.bot_data['total_rotas'] = len(self.rotas_viaveis)
        await send_telegram_message(f"🗺️ Mapa de rotas reconstruído. {self.bot_data['total_rotas']} rotas cripto-cripto serão monitoradas.")
        self.bot_data['progress_status'] = "Pronto para iniciar ciclos de análise."

    def _get_pair_details(self, coin_from, coin_to):
        pair_buy = f"{coin_to}/{coin_from}"
        if pair_buy in self.markets: return pair_buy, 'buy'
        pair_sell = f"{coin_from}/{coin_to}"
        if pair_sell in self.markets: return pair_sell, 'sell'
        return None, None

    async def verificar_oportunidades(self):
        logger.info("Motor 'Antifrágil' (v17.12) iniciado.")
        while True:
            await asyncio.sleep(5)
            if not self.bot_data.get('is_running', True) or self.trade_lock.locked():
                self.bot_data['progress_status'] = f"Pausado. Próxima verificação em 10s."
                await asyncio.sleep(10)
                continue
            self.stats['ciclos_verificacao_total'] += 1
            logger.info(f"Iniciando ciclo de verificação #{self.stats['ciclos_verificacao_total']}...")
            try:
                balance = await self.exchange.fetch_balance()
                saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
                volume_a_usar = (saldo_disponivel * (self.bot_data['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA
                if volume_a_usar < MINIMO_ABSOLUTO_USDT:
                    self.bot_data['progress_status'] = f"Volume de trade ({volume_a_usar:.2f} USDT) abaixo do mínimo. Aguardando."
                    await asyncio.sleep(30)
                    continue
                self.current_cycle_results = []
                total_rotas = len(self.rotas_viaveis)
                for i, cycle_tuple in enumerate(self.rotas_viaveis):
                    self.bot_data['progress_status'] = f"Analisando... Rota {i+1}/{total_rotas}."
                    try:
                        resultado = await self._simular_trade(list(cycle_tuple), volume_a_usar)
                        if resultado:
                            self.current_cycle_results.append(resultado)
                    except Exception as e:
                        self.stats['erros_simulacao'] += 1
                        logger.warning(f"Erro ao simular rota {cycle_tuple}: {e}")
                    await asyncio.sleep(0.1)
                self.ecg_data = sorted(self.current_cycle_results, key=lambda x: x['profit'], reverse=True)
                self.current_cycle_results = []
                logger.info(f"Ciclo de verificação concluído. {len(self.ecg_data)} rotas simuladas com sucesso. {self.stats['erros_simulacao']} erros encontrados e ignorados.")
                self.bot_data['progress_status'] = f"Ciclo concluído. Aguardando próximo ciclo..."
                if self.ecg_data and self.ecg_data[0]['profit'] > self.bot_data['min_profit']:
                    async with self.trade_lock:
                        await self._executar_trade(self.ecg_data[0]['cycle'], volume_a_usar)
            except Exception as e:
                logger.error(f"Erro CRÍTICO no loop de verificação: {e}", exc_info=True)
                await send_telegram_message(f"⚠️ **Erro Grave no Bot:** `{type(e).__name__}: {e}`. Verifique os logs.")
                self.bot_data['progress_status'] = f"Erro crítico. Verifique os logs."

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
            for price, size in orders:
                price, size = Decimal(str(price)), Decimal(str(size))
                if side == 'buy':
                    cost_for_step = remaining_amount
                    if cost_for_step <= price * size:
                        final_traded_amount += cost_for_step / price
                        remaining_amount = Decimal('0')
                        break
                    else:
                        final_traded_amount += size
                        remaining_amount -= price * size
                else:
                    if remaining_amount <= size:
                        final_traded_amount += remaining_amount * price
                        remaining_amount = Decimal('0')
                        break
                    else:
                        final_traded_amount += size * price
                        remaining_amount -= size
            if remaining_amount > 0: return None
            current_amount = final_traded_amount * (1 - TAXA_TAKER)
        lucro_percentual = ((current_amount - volume_inicial) / volume_inicial) * 100
        if lucro_percentual > 0:
            return {'cycle': cycle_path, 'profit': lucro_percentual}
        return None

    async def _executar_trade(self, cycle_path, volume_a_usar):
        logger.info(f"🚀 Oportunidade encontrada. Executando rota: {' -> '.join(cycle_path)}.")
        if self.bot_data['dry_run']:
            lucro_simulado = self.ecg_data[0]['profit']
            await send_telegram_message(f"✅ **Simulação:** Oportunidade encontrada e seria executada. Lucro simulado: `{lucro_simulado:.4f}%`.")
            self.stats['trades_executados'] += 1
            return
        current_amount_asset = volume_a_usar
        try:
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                if not pair_id: raise Exception(f"Par inválido na rota: {coin_from}/{coin_to}")
                orderbook = await self.exchange.fetch_order_book(pair_id)
                market = self.exchange.market(pair_id)
                if side == 'sell':
                    limit_price = Decimal(str(orderbook['bids'][0][0])) / MARGEM_PRECO_TAKER
                    raw_amount_to_trade = current_amount_asset
                else:
                    limit_price = Decimal(str(orderbook['asks'][0][0])) * MARGEM_PRECO_TAKER
                    raw_amount_to_trade = current_amount_asset / limit_price
                amount_to_trade = self.exchange.amount_to_precision(pair_id, raw_amount_to_trade)
                min_amount = Decimal(str(market['limits']['amount']['min']))
                if amount_to_trade < min_amount:
                    raise ValueError(f"Volume calculado `{amount_to_trade}` é muito baixo para o par `{pair_id}`.")
                logger.info(f"Tentando ordem LIMIT: {side.upper()} {amount_to_trade} de {pair_id} @ {limit_price}")
                limit_order = await self.exchange.create_order(symbol=pair_id, type='limit', side=side, amount=amount_to_trade, price=limit_price)
                await asyncio.sleep(3) 
                order_status = await self.exchange.fetch_order(limit_order['id'], pair_id)
                if order_status['status'] != 'closed':
                    logger.warning(f"❌ Ordem LIMIT não preenchida. Cancelando e usando ordem a MERCADO.")
                    await self.exchange.cancel_order(limit_order['id'], pair_id)
                    market_order = await self.exchange.create_market_order(symbol=pair_id, side=side, amount=amount_to_trade)
                    order_status = await self.exchange.fetch_order(market_order['id'], pair_id)
                    logger.info(f"✅ Ordem a MERCADO preenchida com sucesso!")
                filled_amount = Decimal(str(order_status['filled']))
                filled_price = Decimal(str(order_status['average'])) if order_status['average'] else Decimal(str(order_status['price']))
                if side == 'buy':
                    current_amount_asset = filled_amount * (1 - TAXA_TAKER)
                else:
                    current_amount_asset = (filled_amount * filled_price) * (1 - TAXA_TAKER)
            final_amount = current_amount_asset
            lucro_real_percent = ((final_amount - volume_a_usar) / volume_a_usar) * 100
            lucro_real_usdt = final_amount - volume_a_usar
            self.stats['trades_executados'] += 1
            self.stats['lucro_total_sessao'] += lucro_real_usdt
            self.bot_data['daily_profit_usdt'] += lucro_real_usdt
            await send_telegram_message(f"✅ **Arbitragem Executada!**\nRota: `{' -> '.join(cycle_path)}`\nLucro: `{lucro_real_usdt:.4f} USDT` (`{lucro_real_percent:.4f}%`)")
        except Exception as e:
            logger.error(f"❌ Falha na execução do trade: {e}", exc_info=True)
            await send_telegram_message(f"❌ **Falha na Execução:** `{e}`")

async def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem no Telegram: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = "👋 Olá! Sou o Gênesis v17.12. Use /ajuda para ver os comandos."
    await update.message.reply_text(help_text)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    dry_run = context.bot_data.get('dry_run', True)
    status_text = "Em operação" if context.bot_data.get('is_running', True) else "Pausado"
    dry_run_text = "Simulação" if dry_run else "Modo Real"
    response = (f"🤖 **Status Gênesis v17.12:**\n"
                f"**Status:** `{status_text}`\n"
                f"**Modo:** `{dry_run_text}`\n"
                f"**Progresso:** `{context.bot_data.get('progress_status')}`")
    await update.message.reply_text(response, parse_mode="Markdown")

async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine = context.bot_data.get('engine')
    if not engine or not engine.exchange:
        await update.message.reply_text("Engine não inicializada.")
        return
    try:
        balance = await engine.exchange.fetch_balance()
        saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
        await update.message.reply_text(f"📊 Saldo OKX: `{saldo_disponivel:.4f} {MOEDA_BASE_OPERACIONAL}`")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao buscar saldo: {e}")

async def modo_real_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['dry_run'] = False
    await update.message.reply_text("✅ **Modo Real Ativado!**")

async def modo_simulacao_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['dry_run'] = True
    await update.message.reply_text("✅ **Modo Simulação Ativado!**")

async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.bot_data['min_profit'] = Decimal(context.args[0])
        await update.message.reply_text(f"✅ Lucro mínimo definido para `{context.bot_data['min_profit']:.4f}%`.")
    except:
        await update.message.reply_text("❌ Uso: /setlucro <porcentagem>")

async def setvolume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        volume = Decimal(context.args[0])
        if not (0 < volume <= 100): raise ValueError
        context.bot_data['volume_percent'] = volume
        await update.message.reply_text(f"✅ Volume de trade definido para `{volume:.2f}%`.")
    except:
        await update.message.reply_text("❌ Uso: /setvolume <porcentagem entre 1-100>")

async def pausar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['is_running'] = False
    await update.message.reply_text("⏸️ Motor pausado.")

async def retomar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['is_running'] = True
    await update.message.reply_text("▶️ Motor retomado.")

async def set_stoploss_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if context.args[0].lower() == 'off':
            context.bot_data['stop_loss_usdt'] = None
            await update.message.reply_text("✅ Stop Loss desativado.")
        else:
            context.bot_data['stop_loss_usdt'] = -Decimal(context.args[0])
            await update.message.reply_text(f"✅ Stop Loss definido para `{abs(context.bot_data['stop_loss_usdt']):.2f} USDT`.")
    except:
        await update.message.reply_text("❌ Uso: /set_stoploss <valor> ou /set_stoploss off")

async def rotas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine = context.bot_data.get('engine')
    if engine and engine.ecg_data:
        top_rotas = "\n".join([f"`{' -> '.join(r['cycle'])}` ({r['profit']:.4f}%)" for r in engine.ecg_data[:5]])
        await update.message.reply_text(f"📈 **Top 5 Rotas (Simulação):**\n{top_rotas}", parse_mode="Markdown")
    else:
        await update.message.reply_text("Ainda não há dados de rotas.")

async def ajuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📚 **Lista de Comandos:**
`/start` - Mensagem de boas-vindas.
`/status` - Mostra o status atual do bot.
`/saldo` - Exibe o saldo disponível em USDT.
`/modo_real` - Ativa o modo de negociação real.
`/modo_simulacao` - Ativa o modo de simulação.
`/setlucro <%>` - Define o lucro mínimo para executar (ex: `0.1`).
`/setvolume <%>` - Define a porcentagem do saldo a usar (ex: `50`).
`/pausar` - Pausa o motor de arbitragem.
`/retomar` - Retoma o motor.
`/set_stoploss <valor>` - Define stop loss em USDT. Use 'off' para desativar.
`/rotas` - Mostra as 5 rotas mais lucrativas simuladas.
`/ajuda` - Exibe esta lista de comandos.
`/stats` - Estatísticas da sessão.
`/setdepth <n>` - Define a profundidade máxima das rotas (padrão: 3).
`/progresso` - Mostra o status atual do ciclo de análise.
"""
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine = context.bot_data.get('engine')
    if not engine: return
    stats = engine.stats
    uptime = time.strftime("%Hh %Mm %Ss", time.gmtime(time.time() - stats['start_time']))
    response = (f"📊 **Estatísticas:**\n"
                f"**Atividade:** `{uptime}`\n"
                f"**Ciclos:** `{stats['ciclos_verificacao_total']}`\n"
                f"**Trades:** `{stats['trades_executados']}`\n"
                f"**Lucro (Sessão):** `{stats['lucro_total_sessao']:.4f} USDT`")
    await update.message.reply_text(response, parse_mode="Markdown")

async def setdepth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine = context.bot_data.get('engine')
    if not engine: return
    try:
        depth = int(context.args[0])
        if not (MIN_ROUTE_DEPTH <= depth <= 5): raise ValueError
        context.bot_data['max_depth'] = depth
        await engine.construir_rotas(depth)
        await update.message.reply_text(f"✅ Profundidade de rotas definida para `{depth}`.")
    except:
        await update.message.reply_text(f"❌ Uso: /setdepth <número de {MIN_ROUTE_DEPTH} a 5>")
        
async def progresso_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"⚙️ **Progresso:** `{context.bot_data.get('progress_status', 'N/A')}`", parse_mode="Markdown")

async def post_init_tasks(app: Application):
    logger.info("Iniciando motor Gênesis v17.12...")
    engine = GenesisEngine(app)
    app.bot_data['engine'] = engine
    await send_telegram_message("🤖 *Gênesis v17.12 'Ataque aos Erros de Preço' iniciado.*")
    if await engine.inicializar_exchange():
        await engine.construir_rotas(app.bot_data['max_depth'])
        asyncio.create_task(engine.verificar_oportunidades())
    else:
        await send_telegram_message("❌ **ERRO CRÍTICO:** Não foi possível conectar à OKX.")

def main():
    if not TELEGRAM_TOKEN:
        logger.critical("Token do Telegram não encontrado. Verifique a variável de ambiente TELEGRAM_TOKEN.")
        return
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    command_map = {
        "start": start_command, "status": status_command, "saldo": saldo_command,
        "modo_real": modo_real_command, "modo_simulacao": modo_simulacao_command,
        "setlucro": setlucro_command, "setvolume": setvolume_command,
        "pausar": pausar_command, "retomar": retomar_command,
        "set_stoploss": set_stoploss_command, 
        "rotas": rotas_command,
        "ajuda": ajuda_command,
        "stats": stats_command,
        "setdepth": setdepth_command,
        "progresso": progresso_command,
    }
    for command, handler in command_map.items():
        application.add_handler(CommandHandler(command, handler))

    application.post_init = post_init_tasks
    logger.info("Iniciando bot do Telegram...")
    application.run_polling()

if __name__ == "__main__":
    main()
