# -*- coding: utf-8 -*-
# Gênesis v11.31 - OKX (CORREÇÃO CRÍTICA DE LÓGICA E COMANDOS)
# Resolvido o problema de loop travado e adicionado o comando /setdepth.

import os
import asyncio
import logging
from decimal import Decimal, getcontext
import time
from datetime import datetime

# === IMPORTAÇÃO CCXT E TELEGRAM ===
try:
    import ccxt.async_support as ccxt
    from telegram import Update, Bot
    from telegram.ext import Application, CommandHandler, ContextTypes
except ImportError:
    print("Erro: Bibliotecas essenciais não instaladas.")
    ccxt = None
    Bot = None

# ==============================================================================
# 1. CONFIGURAÇÃO GLOBAL E INICIALIZAÇÃO
# ==============================================================================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
getcontext().prec = 30

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSPHRASE = os.getenv("OKX_API_PASSPHRASE")

TAXA_TAKER = Decimal("0.001")
MIN_PROFIT_DEFAULT = Decimal("0.0005")
MARGEM_DE_SEGURANCA = Decimal("0.995")
MOEDA_BASE_OPERACIONAL = 'USDT'
MINIMO_ABSOLUTO_USDT = Decimal("3.1")
MIN_ROUTE_DEPTH = 2
MAX_ROUTE_DEPTH_DEFAULT = 3

class GenesisEngine:
    def __init__(self, application: Application):
        self.app = application
        self.bot_data = application.bot_data
        self.exchange = None
        self.bot_data.setdefault('is_running', True)
        self.bot_data.setdefault('min_profit', MIN_PROFIT_DEFAULT)
        self.bot_data.setdefault('dry_run', True)
        self.bot_data.setdefault('volume_percent', Decimal("100.0"))
        self.bot_data.setdefault('max_depth', MAX_ROUTE_DEPTH_DEFAULT)
        self.bot_data.setdefault('daily_profit_usdt', Decimal('0'))
        self.bot_data.setdefault('stop_loss_usdt', None)
        self.bot_data.setdefault('last_reset_day', datetime.utcnow().day)
        self.markets = {}
        self.graph = {}
        self.rotas_viaveis = {}
        self.ecg_data = []
        self.trade_lock = asyncio.Lock()
        self.stats = {'start_time': time.time(), 'ciclos_verificacao_total': 0, 'trades_executados': 0, 'lucro_total_sessao': Decimal('0')}

    async def inicializar_exchange(self):
        if not ccxt: return False
        if not all([OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE]):
            await send_telegram_message("❌ Falha crítica: Verifique as chaves da API da OKX na Heroku.")
            return False
        try:
            self.exchange = ccxt.okx({'apiKey': OKX_API_KEY, 'secret': OKX_API_SECRET, 'password': OKX_API_PASSPHRASE, 'options': {'defaultType': 'spot'}})
            self.markets = await self.exchange.load_markets()
            logger.info(f"Conectado à OKX. {len(self.markets)} mercados carregados.")
            return True
        except Exception as e:
            logger.critical(f"❌ Falha ao conectar com a OKX: {e}", exc_info=True)
            await send_telegram_message(f"❌ Erro de Conexão com a OKX: `{type(e).__name__}`.")
            if self.exchange: await self.exchange.close()
            return False

    async def construir_rotas(self, max_depth: int):
        logger.info(f"Construindo mapa (Profundidade: {max_depth})...")
        self.graph = {}
        active_markets = {s: m for s, m in self.markets.items() if m.get('active') and m.get('base') and m.get('quote')}
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
                    todas_as_rotas.append(path + [v])
                elif v not in path:
                    encontrar_ciclos_dfs(v, path + [v], depth + 1)
        
        encontrar_ciclos_dfs(MOEDA_BASE_OPERACIONAL, [MOEDA_BASE_OPERACIONAL], 1)
        self.rotas_viaveis = {tuple(rota) for rota in todas_as_rotas if self._validar_rota_completa(rota)}
        self.bot_data['total_rotas'] = len(self.rotas_viaveis)
        await send_telegram_message(f"🗺️ Mapa de rotas reconstruído. {self.bot_data['total_rotas']} rotas serão monitoradas.")

    def _validar_rota_completa(self, cycle_path):
        for i in range(len(cycle_path) - 1):
            pair_id, _ = self._get_pair_details(cycle_path[i], cycle_path[i+1])
            if not pair_id or not self.markets.get(pair_id, {}).get('active'): return False
        return True

    def _get_pair_details(self, coin_from, coin_to):
        pair_buy = f"{coin_to}/{coin_from}"
        if pair_buy in self.markets: return pair_buy, 'buy'
        pair_sell = f"{coin_from}/{coin_to}"
        if pair_sell in self.markets: return pair_sell, 'sell'
        return None, None

    async def verificar_oportunidades(self):
        logger.info("Motor Oportunista (OKX) iniciado.")
        while True:
            await asyncio.sleep(3) # Pequeno aumento para dar fôlego à API
            if datetime.utcnow().day != self.bot_data['last_reset_day']:
                self.bot_data['daily_profit_usdt'] = Decimal('0')
                self.bot_data['last_reset_day'] = datetime.utcnow().day
                await send_telegram_message("📅 **Novo Dia!** Contador de lucro zerado.")
            
            stop_loss_limit = self.bot_data.get('stop_loss_usdt')
            if stop_loss_limit is not None and self.bot_data['daily_profit_usdt'] <= -stop_loss_limit:
                if self.bot_data['is_running']:
                    self.bot_data['is_running'] = False
                    await send_telegram_message(f"🛑 **STOP LOSS ATINGIDO!** O bot foi pausado.")
                continue

            if not self.bot_data.get('is_running', True) or self.trade_lock.locked(): continue
            
            try:
                self.stats['ciclos_verificacao_total'] += 1
                balance = await self.exchange.fetch_balance()
                saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
                volume_a_usar = (saldo_disponivel * (self.bot_data['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA

                if volume_a_usar < MINIMO_ABSOLUTO_USDT:
                    logger.info(f"Volume de trade ({volume_a_usar:.2f} USDT) abaixo do mínimo. Aguardando.")
                    await asyncio.sleep(10)
                    continue

                # CORREÇÃO CRÍTICA: Processar em lotes para não sobrecarregar
                temp_results = []
                rotas_a_verificar = list(self.rotas_viaveis)
                for i in range(0, len(rotas_a_verificar), 100): # Lotes de 100
                    lote = rotas_a_verificar[i:i+100]
                    tasks = [self._simular_trade(list(cycle), volume_a_usar) for cycle in lote]
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for res in results:
                        if isinstance(res, dict):
                            temp_results.append(res)
                        elif isinstance(res, Exception):
                            logger.warning(f"Erro na simulação de uma rota: {res}")
                
                self.ecg_data = sorted(temp_results, key=lambda x: x['profit'], reverse=True)
                logger.info(f"Ciclo de verificação concluído. {len(self.ecg_data)} rotas simuladas com sucesso.")

                if self.ecg_data and self.ecg_data[0]['profit'] > self.bot_data['min_profit']:
                    async with self.trade_lock:
                        await self._executar_trade(self.ecg_data[0]['cycle'], volume_a_usar)
            except Exception as e:
                logger.error(f"Erro CRÍTICO no loop de verificação: {e}", exc_info=True)
                await send_telegram_message(f"⚠️ **Erro Grave no Bot:** `{type(e).__name__}`. Verifique os logs.")

    async def _simular_trade(self, cycle_path, volume_inicial):
        current_amount = volume_inicial
        for i in range(len(cycle_path) - 1):
            pair_id, side = self._get_pair_details(cycle_path[i], cycle_path[i+1])
            if not pair_id: raise ValueError(f"Par inválido na rota: {cycle_path[i]}/{cycle_path[i+1]}")
            
            orderbook = await self.exchange.fetch_order_book(pair_id)
            orders = orderbook['asks'] if side == 'buy' else orderbook['bids']
            if not orders: return None # Rota sem liquidez, não é um erro, apenas inviável

            amount_traded, total_cost, remaining = Decimal('0'), Decimal('0'), current_amount
            if side == 'buy':
                for price, size in orders:
                    price, size = Decimal(str(price)), Decimal(str(size))
                    cost = price * size
                    if remaining >= cost:
                        total_cost += cost; amount_traded += size; remaining -= cost
                    else:
                        amount_traded += remaining / price; total_cost += remaining; remaining = Decimal('0'); break
                current_amount = amount_traded * (1 - TAXA_TAKER)
            else: # sell
                for price, size in orders:
                    price, size = Decimal(str(price)), Decimal(str(size))
                    if remaining >= size:
                        total_cost += price * size; amount_traded += size; remaining -= size
                    else:
                        total_cost += price * remaining; amount_traded += remaining; remaining = Decimal('0'); break
                current_amount = total_cost * (1 - TAXA_TAKER)
            
            if remaining > 0: return None # Slippage muito alto, rota inviável
        
        lucro_percentual = ((current_amount - volume_inicial) / volume_inicial) * 100
        return {'cycle': cycle_path, 'profit': lucro_percentual}

    async def _executar_trade(self, cycle_path, volume_a_usar):
        # (Esta função permanece a mesma)
        pass # Cole a função _executar_trade da versão anterior aqui

# --- Comandos do Telegram (v11.31) ---

async def ajuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 **Lista de Comandos - Gênesis v11.31**\n\n"
        "**GESTÃO E STATUS**\n"
        "`/status` - Painel de controle principal.\n"
        "`/stats` - Estatísticas da sessão atual.\n"
        "`/saldo` - Verifica os saldos na OKX.\n"
        "`/pausar` | `/retomar` - Pausa ou retoma o bot.\n\n"
        "**MODO DE OPERAÇÃO**\n"
        "`/modo_real` | `/modo_simulacao`\n\n"
        "**CONFIGURAÇÕES**\n"
        "`/setlucro [valor]` - Ex: `/setlucro 0.05`\n"
        "`/setvolume [valor]` - Ex: `/setvolume 50`\n"
        "`/set_stoploss [valor]` - Ex: `/set_stoploss 10`\n"
        "`/setdepth [valor]` - Ex: `/setdepth 4` (Reconstrói as rotas)\n\n"
        "**ANÁLISE**\n"
        "`/rotas` - **[DEBUG]** Mostra as 10 melhores rotas e seu resultado."
    )
    await update.message.reply_text(msg, parse_mode='Markdown')

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Olá! Gênesis v11.31 (OKX) online. Use /ajuda para ver os comandos.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine: await update.message.reply_text("Motor não inicializado."); return
    bd = context.bot_data
    status_text = "▶️ Rodando" if bd.get('is_running') else "⏸️ Pausado"
    stop_loss = bd.get('stop_loss_usdt')
    stop_loss_status = f"`-{stop_loss} USDT`" if stop_loss else "`Não definido`"
    msg = (f"📊 **Painel de Controle - Gênesis v11.31 (OKX)**\n\n"
           f"**Estado:** `{status_text}`\n"
           f"**Modo:** `{'Simulação' if bd.get('dry_run') else '🔴 REAL'}`\n"
           f"**Lucro Mínimo:** `{bd.get('min_profit')}%`\n"
           f"**Volume por Trade:** `{bd.get('volume_percent')}%`\n"
           f"**Profundidade:** `{bd.get('max_depth')}`\n"
           f"**Lucro Diário:** `{bd.get('daily_profit_usdt'):.4f} USDT`\n"
           f"**Stop Loss:** {stop_loss_status}\n"
           f"**Rotas Monitoradas:** `{bd.get('total_rotas', 0)}`")
    await update.message.reply_text(msg, parse_mode='Markdown')

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine: await update.message.reply_text("Motor não inicializado."); return
    uptime_seconds = time.time() - engine.stats['start_time']
    m, s = divmod(uptime_seconds, 60); h, m = divmod(m, 60)
    uptime_str = f"{int(h)}h {int(m)}m {int(s)}s"
    msg = (f"📈 **Estatísticas da Sessão**\n\n"
           f"**Tempo Ativo:** `{uptime_str}`\n"
           f"**Ciclos de Verificação:** `{engine.stats['ciclos_verificacao_total']}`\n"
           f"**Trades Executados:** `{engine.stats['trades_executados']}`\n"
           f"**Lucro Total da Sessão:** `{engine.stats['lucro_total_sessao']:.4f} USDT`")
    await update.message.reply_text(msg, parse_mode='Markdown')

async def rotas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine or not engine.ecg_data:
        await update.message.reply_text("⏳ Dados de simulação ainda não disponíveis. Tente novamente em alguns segundos.")
        return
    top_10_results = engine.ecg_data[:10]
    msg = "📡 **[DEBUG] Análise de Rotas (Top 10)**\n\n"
    if not top_10_results:
        await update.message.reply_text("🔎 Nenhuma rota foi simulada com sucesso neste ciclo.")
        return
    for result in top_10_results:
        lucro = result['profit']
        emoji = "🔼" if lucro > 0 else "🔽"
        rota_fmt = ' -> '.join(result['cycle'])
        msg += f"**- Rota:** `{rota_fmt}`\n"
        msg += f"  **Resultado Bruto:** `{emoji} {lucro:.4f}%`\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def setdepth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """NOVO E CORRIGIDO: Define a profundidade e reconstrói as rotas."""
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine: await update.message.reply_text("Motor não inicializado."); return
    try:
        depth = int(context.args[0])
        if MIN_ROUTE_DEPTH <= depth <= 5: # Limite de 5 para não sobrecarregar
            context.bot_data['max_depth'] = depth
            await update.message.reply_text(f"✅ Profundidade definida para **{depth}**. Reconstruindo mapa de rotas, aguarde...")
            await engine.construir_rotas(depth)
            await status_command(update, context)
        else:
            await update.message.reply_text(f"⚠️ A profundidade deve ser entre {MIN_ROUTE_DEPTH} e 5.")
    except (IndexError, ValueError):
        await update.message.reply_text(f"⚠️ Uso: `/setdepth [número]` (ex: `/setdepth 3`)")

async def retomar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['is_running'] = True
    await update.message.reply_text("✅ **Bot retomado.**")
    await status_command(update, context)

# (Cole aqui os outros comandos que não mudaram: saldo, set_stoploss, modo_real, etc.)

async def post_init_tasks(app: Application):
    logger.info("Iniciando motor Gênesis para OKX...")
    engine = GenesisEngine(app)
    app.bot_data['engine'] = engine
    await send_telegram_message("🤖 *Gênesis v11.31 (OKX) iniciado.*\nUse /ajuda para ver os comandos.")
    if await engine.inicializar_exchange():
        await engine.construir_rotas(app.bot_data['max_depth'])
        asyncio.create_task(engine.verificar_oportunidades())
        logger.info("Motor e tarefas de fundo iniciadas.")
    else:
        await send_telegram_message("❌ **ERRO CRÍTICO:** Não foi possível conectar à OKX.")
        if engine.exchange: await engine.exchange.close()

def main():
    if not TELEGRAM_TOKEN: logger.critical("Token do Telegram não encontrado."); return
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
        "setdepth": setdepth_command, # CORRIGIDO
    }
    # (adicione os handlers que faltam aqui, como saldo_command, etc.)
    for command, handler in command_map.items():
        application.add_handler(CommandHandler(command, handler))

    application.post_init = post_init_tasks
    logger.info("Iniciando bot do Telegram...")
    application.run_polling()

if __name__ == "__main__":
    main()
