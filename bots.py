# -*- coding: utf-8 -*-
# Gênesis v17.28 - "Estratégia Anti-Falha"
# Bot 1 (OKX) - v4.0: Implementado try...finally para resolver deadlocks e lógica de reversão.

import os
import asyncio
import logging
from decimal import Decimal, getcontext
import time
from datetime import datetime

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

# Variáveis de Ambiente (Lidas do Heroku)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD")

# Parâmetros de Custo e Segurança
TAXA_TAKER = Decimal("0.001")
MIN_PROFIT_DEFAULT = Decimal("0.4")
MARGEM_DE_SEGURANCA = Decimal("0.995")
MOEDA_BASE_OPERACIONAL = 'USDT'
MINIMO_ABSOLUTO_USDT = Decimal("3.1")
MIN_ROUTE_DEPTH = 3
MAX_ROUTE_DEPTH_DEFAULT = 3
FIAT_CURRENCIES = {'BRL', 'USD', 'EUR', 'JPY', 'GBP', 'AUD', 'CAD', 'CHF', 'CNY'}

# ==============================================================================
# 2. CLASSE DO MOTOR DE ARBITRAGEM (GenesisEngine)
# ==============================================================================
class GenesisEngine:
    def __init__(self, application: Application):
        self.app = application
        self.bot_data = application.bot_data
        self.exchange = None
        self.trade_lock = asyncio.Lock()

        # Configurações do Bot com valores padrão
        self.bot_data.setdefault('is_running', True)
        self.bot_data.setdefault('min_profit', MIN_PROFIT_DEFAULT)
        self.bot_data.setdefault('dry_run', True)
        self.bot_data.setdefault('volume_percent', Decimal("100.0"))
        self.bot_data.setdefault('max_depth', MAX_ROUTE_DEPTH_DEFAULT)
        
        # Dados Operacionais e Estatísticas
        self.markets = {}
        self.graph = {}
        self.rotas_viaveis = []
        self.ecg_data = []
        self.stats = {'start_time': time.time(), 'ciclos_verificacao_total': 0, 'trades_executados': 0, 'lucro_total_sessao': Decimal('0'), 'erros_simulacao': 0, 'falhas_execucao': 0}
        self.bot_data['progress_status'] = "Iniciando..."

    async def inicializar_exchange(self):
        if not all([OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSWORD]):
            await send_telegram_message("❌ Falha crítica: Verifique as chaves da API da OKX na Heroku.")
            return False
        try:
            self.exchange = ccxt.okx({
                'apiKey': OKX_API_KEY, 'secret': OKX_API_SECRET, 'password': OKX_API_PASSWORD,
                'options': {'defaultType': 'spot'},
            })
            self.markets = await self.exchange.load_markets()
            logger.info(f"Conectado à OKX. {len(self.markets)} mercados carregados.")
            return True
        except Exception as e:
            logger.critical(f"❌ Falha ao conectar com a OKX: {e}", exc_info=True)
            await send_telegram_message(f"❌ Erro de Conexão com a OKX: `{type(e).__name__}: {e}`.")
            if self.exchange: await self.exchange.close()
            return False

    async def construir_rotas(self, max_depth: int):
        # (Esta função permanece a mesma, é robusta)
        self.bot_data['progress_status'] = "Construindo mapa de rotas..."
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
        await send_telegram_message(f"🗺️ Mapa de rotas reconstruído. {len(self.rotas_viaveis)} rotas serão monitoradas.")
        self.bot_data['progress_status'] = "Pronto para iniciar ciclos."

    def _get_pair_details(self, coin_from, coin_to):
        pair_buy = f"{coin_to}/{coin_from}"
        if pair_buy in self.markets: return pair_buy, 'buy'
        pair_sell = f"{coin_from}/{coin_to}"
        if pair_sell in self.markets: return pair_sell, 'sell'
        return None, None

    async def verificar_oportunidades(self):
        logger.info("Motor 'Anti-Falha' (v4.0) iniciado.")
        while True:
            await asyncio.sleep(1) # Ciclo mais rápido
            if not self.bot_data.get('is_running', True):
                self.bot_data['progress_status'] = "Pausado."
                await asyncio.sleep(10)
                continue
            
            if self.trade_lock.locked():
                self.bot_data['progress_status'] = "Aguardando liberação de trava de segurança..."
                await asyncio.sleep(5)
                continue

            self.stats['ciclos_verificacao_total'] += 1
            try:
                balance = await self.exchange.fetch_balance()
                saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
                volume_a_usar = (saldo_disponivel * (self.bot_data['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA
                if volume_a_usar < MINIMO_ABSOLUTO_USDT:
                    self.bot_data['progress_status'] = f"Volume ({volume_a_usar:.2f} USDT) abaixo do mínimo. Aguardando."
                    await asyncio.sleep(20)
                    continue
                
                self.ecg_data = []
                total_rotas = len(self.rotas_viaveis)
                for i, cycle_tuple in enumerate(self.rotas_viaveis):
                    self.bot_data['progress_status'] = f"Analisando... Rota {i+1}/{total_rotas}."
                    try:
                        resultado = await self._simular_trade(list(cycle_tuple), volume_a_usar)
                        if resultado: self.ecg_data.append(resultado)
                    except Exception as e:
                        self.stats['erros_simulacao'] += 1
                    await asyncio.sleep(0.05) # Simulação mais rápida

                if self.ecg_data:
                    self.ecg_data.sort(key=lambda x: x['profit'], reverse=True)
                    melhor_rota = self.ecg_data[0]
                    if melhor_rota['profit'] > self.bot_data['min_profit']:
                        await self._executar_trade(melhor_rota['cycle'], volume_a_usar)
                
                self.bot_data['progress_status'] = f"Ciclo #{self.stats['ciclos_verificacao_total']} concluído. Aguardando..."

            except Exception as e:
                logger.error(f"Erro CRÍTICO no loop de verificação: {e}", exc_info=True)
                await send_telegram_message(f"⚠️ **Erro Grave no Bot:** `{type(e).__name__}`. Verifique os logs.")
                self.bot_data['progress_status'] = f"Erro crítico. Reiniciando em 60s."
                await asyncio.sleep(60)

    async def _simular_trade(self, cycle_path, volume_inicial):
        # (Esta função permanece a mesma, é robusta)
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
                await send_telegram_message(f"✅ **Simulação:** Oportunidade encontrada. Lucro líquido: `{lucro_simulado:.4f}%`.")
                self.stats['trades_executados'] += 1
                return

            # --- Início da Execução Real ---
            moedas_presas = [] # Guarda (moeda, quantidade) se uma perna falhar
            current_amount_asset = volume_a_usar
            
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                
                if not pair_id: raise Exception(f"Par inválido na rota: {coin_from}/{coin_to}")
                
                market = self.exchange.market(pair_id)
                
                # Tenta criar a ordem
                try:
                    # Para compra, a quantidade é na moeda base. Para venda, é a quantidade que temos.
                    amount_to_trade = current_amount_asset
                    
                    # Verifica os limites ANTES de enviar a ordem
                    min_amount = Decimal(str(market['limits']['amount']['min']))
                    if amount_to_trade < min_amount and side == 'sell':
                         raise ValueError(f"Quantidade ({amount_to_trade:.4f} {market['base']}) abaixo do mínimo do par ({min_amount}).")

                    order = await self.exchange.create_market_order(symbol=pair_id, side=side, amount=amount_to_trade)
                    
                    # Aguarda a ordem ser preenchida
                    await asyncio.sleep(1.5) # Espera para a API da corretora atualizar
                    order_status = await self.exchange.fetch_order(order['id'], pair_id)

                    if order_status['status'] != 'closed':
                        raise Exception(f"Ordem {order['id']} não foi preenchida a tempo. Status: {order_status['status']}")

                    filled_amount = Decimal(str(order_status['filled']))
                    filled_price = Decimal(str(order_status['average']))
                    
                    # Atualiza o saldo para a próxima perna
                    if side == 'buy':
                        current_amount_asset = filled_amount * (1 - TAXA_TAKER)
                        moedas_presas.append({'symbol': coin_to, 'amount': current_amount_asset})
                    else: # side == 'sell'
                        current_amount_asset = (filled_amount * filled_price) * (1 - TAXA_TAKER)
                        moedas_presas.pop() # Remove a moeda anterior, pois foi vendida com sucesso

                except Exception as leg_error:
                    self.stats['falhas_execucao'] += 1
                    await send_telegram_message(f"🔴 **FALHA NA PERNA {i+1} da Rota!**\n`{' -> '.join(cycle_path)}`\n**Erro:** `{leg_error}`")
                    
                    # --- LÓGICA DE REVERSÃO (VENDA DE PÂNICO) ---
                    if moedas_presas:
                        ativo_preso = moedas_presas[-1]
                        await send_telegram_message(f"⚠️ **CAPITAL PRESO!**\nAtivo: `{ativo_preso['amount']:.4f} {ativo_preso['symbol']}`.\n**Iniciando venda de emergência para USDT...**")
                        try:
                            reversal_pair, _ = self._get_pair_details(ativo_preso['symbol'], 'USDT')
                            if reversal_pair:
                                await self.exchange.create_market_sell_order(symbol=reversal_pair, amount=ativo_preso['amount'])
                                await send_telegram_message("✅ **Venda de Emergência Executada!** Saldo recuperado em USDT.")
                            else:
                                await send_telegram_message("❌ **Falha na Venda de Emergência:** Par com USDT não encontrado.")
                        except Exception as reversal_error:
                            await send_telegram_message(f"❌ **FALHA CRÍTICA NA VENDA DE EMERGÊNCIA:** `{reversal_error}`. **VERIFIQUE A CONTA MANUALMENTE!**")
                    return # Aborta a execução da rota

            # Se chegou aqui, a rota foi um sucesso
            final_amount = current_amount_asset
            lucro_real_percent = ((final_amount - volume_a_usar) / volume_a_usar) * 100
            lucro_real_usdt = final_amount - volume_a_usar
            self.stats['trades_executados'] += 1
            self.stats['lucro_total_sessao'] += lucro_real_usdt
            await send_telegram_message(f"✅ **Arbitragem Concluída!**\nRota: `{' -> '.join(cycle_path)}`\nLucro Líquido: `{lucro_real_usdt:.4f} USDT` (`{lucro_real_percent:.4f}%`)")

        finally:
            self.trade_lock.release() # Garante que a trava seja liberada, não importa o que aconteça

# ==============================================================================
# 3. FUNÇÕES E COMANDOS DO TELEGRAM
# ==============================================================================
async def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem no Telegram: {e}")

# (Todos os comandos do Telegram permanecem os mesmos da v3.3, são robustos)
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("👋 Olá! Sou o Gênesis v4.0 'Anti-Falha'. Use /ajuda para ver os comandos.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    dry_run = context.bot_data.get('dry_run', True)
    status_text = "Em operação" if context.bot_data.get('is_running', True) else "Pausado"
    dry_run_text = "Simulação" if dry_run else "Modo Real"
    response = (
        f"🤖 **Status do Gênesis v4.0:**\n"
        f"**Status:** `{status_text}`\n"
        f"**Modo:** `{dry_run_text}`\n"
        f"**Lucro Mínimo:** `{context.bot_data.get('min_profit'):.4f}%`\n"
        f"**Volume de Trade:** `{context.bot_data.get('volume_percent'):.2f}%` do saldo\n"
        f"**Profundidade de Rotas:** `{context.bot_data.get('max_depth')}`\n\n"
        f"**Progresso:** `{context.bot_data.get('progress_status')}`"
    )
    await update.message.reply_text(response, parse_mode="Markdown")

async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine = context.bot_data.get('engine')
    if not engine or not engine.exchange: await update.message.reply_text("Engine não inicializada."); return
    try:
        balance = await engine.exchange.fetch_balance()
        saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
        await update.message.reply_text(f"📊 Saldo OKX: `{saldo_disponivel:.4f} {MOEDA_BASE_OPERACIONAL}`", parse_mode="Markdown")
    except Exception as e: await update.message.reply_text(f"❌ Erro ao buscar saldo: {e}")

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
    except: await update.message.reply_text("❌ Uso: /setlucro <porcentagem>")

async def setvolume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        volume = Decimal(context.args[0])
        if not (0 < volume <= 100): raise ValueError
        context.bot_data['volume_percent'] = volume
        await update.message.reply_text(f"✅ Volume de trade definido para `{volume:.2f}%`.")
    except: await update.message.reply_text("❌ Uso: /setvolume <porcentagem entre 1-100>")

async def pausar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['is_running'] = False
    await update.message.reply_text("⏸️ Motor pausado.")

async def retomar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['is_running'] = True
    await update.message.reply_text("▶️ Motor retomado.")

async def rotas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine = context.bot_data.get('engine')
    if engine and engine.ecg_data:
        top_rotas = "\n".join([f"`{' -> '.join(r['cycle'])}` (Lucro: {r['profit']:.4f}%)" for r in engine.ecg_data[:5]])
        await update.message.reply_text(f"📈 **Top 5 Rotas (Simulação):**\n{top_rotas}", parse_mode="Markdown")
    else: await update.message.reply_text("Ainda não há dados de rotas.")

async def ajuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 **Comandos:**\n"
        "`/status` - Status atual.\n"
        "`/saldo` - Saldo em USDT.\n"
        "`/modo_real` ou `/modo_simulacao`\n"
        "`/setlucro <%>` (ex: 0.4)\n"
        "`/setvolume <%>` (ex: 100)\n"
        "`/pausar` ou `/retomar`\n"
        "`/rotas` - Top 5 rotas simuladas.\n"
        "`/stats` - Estatísticas da sessão.\n"
        "`/setdepth <n>` (3 a 5)",
        parse_mode="Markdown"
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine = context.bot_data.get('engine')
    if not engine: return
    stats = engine.stats
    uptime = time.strftime("%Hh %Mm %Ss", time.gmtime(time.time() - stats['start_time']))
    response = (f"📊 **Estatísticas (v4.0):**\n"
                f"**Atividade:** `{uptime}`\n"
                f"**Ciclos:** `{stats['ciclos_verificacao_total']}`\n"
                f"**Trades (Sucesso):** `{stats['trades_executados']}`\n"
                f"**Falhas (Execução):** `{stats['falhas_execucao']}`\n"
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
    except: await update.message.reply_text(f"❌ Uso: /setdepth <número de {MIN_ROUTE_DEPTH} a 5>")
        
async def progresso_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"⚙️ **Progresso:** `{context.bot_data.get('progress_status', 'N/A')}`", parse_mode="Markdown")

# ==============================================================================
# 4. FUNÇÃO PRINCIPAL DE INICIALIZAÇÃO
# ==============================================================================
async def post_init_tasks(app: Application):
    logger.info("Iniciando motor Gênesis v4.0 'Anti-Falha'...")
    engine = GenesisEngine(app)
    app.bot_data['engine'] = engine
    await send_telegram_message("🤖 *Gênesis v4.0 'Anti-Falha' iniciado.*")
    if await engine.inicializar_exchange():
        await engine.construir_rotas(app.bot_data['max_depth'])
        asyncio.create_task(engine.verificar_oportunidades())
    else:
        await send_telegram_message("❌ **ERRO CRÍTICO:** Não foi possível conectar à OKX.")

def main():
    if not TELEGRAM_TOKEN:
        logger.critical("Token do Telegram não encontrado.")
        return
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    command_map = {
        "start": start_command, "status": status_command, "saldo": saldo_command,
        "modo_real": modo_real_command, "modo_simulacao": modo_simulacao_command,
        "setlucro": setlucro_command, "setvolume": setvolume_command,
        "pausar": pausar_command, "retomar": retomar_command,
        "rotas": rotas_command, "ajuda": ajuda_command, "stats": stats_command,
        "setdepth": setdepth_command, "progresso": progresso_command,
    }
    for command, handler in command_map.items():
        application.add_handler(CommandHandler(command, handler))

    application.post_init = post_init_tasks
    logger.info("Iniciando bot do Telegram...")
    application.run_polling()

if __name__ == "__main__":
    main()
