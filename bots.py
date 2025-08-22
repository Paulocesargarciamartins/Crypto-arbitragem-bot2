# -*- coding: utf-8 -*-
# Gênesis v17.0 - "Antifrágil" (Oficialmente v13.0.0 no repositório)
# Arquitetura reescrita para resiliência, transparência e confiabilidade.
# O motor de análise agora é sequencial e à prova de falhas.

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
    from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
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
MIN_PROFIT_DEFAULT = Decimal("0.05") # Aumentado para um valor mais realista
MARGEM_DE_SEGURANCA = Decimal("0.995")
MOEDA_BASE_OPERACIONAL = 'USDT'
MINIMO_ABSOLUTO_USDT = Decimal("3.1")
MIN_ROUTE_DEPTH = 3 # Aumentado para evitar rotas degeneradas
MAX_ROUTE_DEPTH_DEFAULT = 3

# Lista de moedas fiduciárias para serem ignoradas
FIAT_CURRENCIES = {'BRL', 'USD', 'EUR', 'JPY', 'GBP', 'AUD', 'CAD', 'CHF', 'CNY'}

# ==============================================================================
# 2. CLASSE DO MOTOR DE ARBITRAGEM (GenesisEngine)
# ==============================================================================
class GenesisEngine:
    def __init__(self, application: Application):
        self.app = application
        self.bot_data = application.bot_data
        self.exchange = None
        
        # Configurações do Bot
        self.bot_data.setdefault('is_running', True)
        self.bot_data.setdefault('min_profit', MIN_PROFIT_DEFAULT)
        self.bot_data.setdefault('dry_run', True)
        self.bot_data.setdefault('volume_percent', Decimal("100.0"))
        self.bot_data.setdefault('max_depth', MAX_ROUTE_DEPTH_DEFAULT)
        self.bot_data.setdefault('stop_loss_usdt', None)
        
        # Dados Operacionais
        self.markets = {}
        self.graph = {}
        self.rotas_viaveis = []
        self.ecg_data = []
        self.trade_lock = asyncio.Lock()
        
        # Status e Estatísticas
        self.bot_data.setdefault('daily_profit_usdt', Decimal('0'))
        self.bot_data.setdefault('last_reset_day', datetime.utcnow().day)
        self.stats = {'start_time': time.time(), 'ciclos_verificacao_total': 0, 'trades_executados': 0, 'lucro_total_sessao': Decimal('0'), 'erros_simulacao': 0}
        self.bot_data['progress_status'] = "Iniciando..."

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
        self.bot_data['progress_status'] = "Construindo mapa de rotas..."
        logger.info(f"Construindo mapa (Profundidade: {max_depth})...")
        self.graph = {}
        
        # Filtra mercados ativos e que não envolvam moedas fiduciárias
        active_markets = {
            s: m for s, m in self.markets.items() 
            if m.get('active') and m.get('base') and m.get('quote') 
            and m['base'] not in FIAT_CURRENCIES and m['quote'] not in FIAT_CURRENCIES
        }
        
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
                    if len(set(rota)) == len(rota) -1: # Garante que não há ciclos internos
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
        logger.info("Motor 'Antifrágil' (v17) iniciado.")
        while True:
            await asyncio.sleep(5) # Pausa entre os ciclos completos
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

                temp_results = []
                total_rotas = len(self.rotas_viaveis)
                
                # === MOTOR SEQUENCIAL E RESILIENTE ===
                for i, cycle_tuple in enumerate(self.rotas_viaveis):
                    self.bot_data['progress_status'] = f"Analisando... Rota {i+1}/{total_rotas}."
                    
                    try:
                        resultado = await self._simular_trade(list(cycle_tuple), volume_a_usar)
                        if resultado:
                            temp_results.append(resultado)
                    except Exception as e:
                        self.stats['erros_simulacao'] += 1
                        logger.warning(f"Erro ao simular rota {cycle_tuple}: {e}")
                    
                    await asyncio.sleep(0.1) # Pausa gentil para a API entre cada rota

                self.ecg_data = sorted(temp_results, key=lambda x: x['profit'], reverse=True)
                logger.info(f"Ciclo de verificação concluído. {len(self.ecg_data)} rotas simuladas com sucesso. {self.stats['erros_simulacao']} erros encontrados e ignorados.")
                self.bot_data['progress_status'] = f"Ciclo concluído. Aguardando próximo ciclo..."

                if self.ecg_data and self.ecg_data[0]['profit'] > self.bot_data['min_profit']:
                    async with self.trade_lock:
                        await self._executar_trade(self.ecg_data[0]['cycle'], volume_a_usar)

            except Exception as e:
                logger.error(f"Erro CRÍTICO no loop de verificação: {e}", exc_info=True)
                await send_telegram_message(f"⚠️ **Erro Grave no Bot:** `{type(e).__name__}`. Verifique os logs.")
                self.bot_data['progress_status'] = f"Erro crítico. Verifique os logs."

    async def _simular_trade(self, cycle_path, volume_inicial):
        current_amount = volume_inicial
        for i in range(len(cycle_path) - 1):
            pair_id, side = self._get_pair_details(cycle_path[i], cycle_path[i+1])
            if not pair_id: return None # Rota inválida, ignora
            
            orderbook = await self.exchange.fetch_order_book(pair_id)
            orders = orderbook['asks'] if side == 'buy' else orderbook['bids']
            
            # Princípio "Antifrágil": Se não há liquidez, a rota é inviável. Não é um erro.
            if not orders:
                return None

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
        logger.info(f"🚀 Oportunidade encontrada. Executando rota: {' -> '.join(cycle_path)}.")
        
        # 1. Trava de segurança para modo de simulação
        if self.bot_data['dry_run']:
            lucro_simulado = self.ecg_data[0]['profit']
            await send_telegram_message(f"✅ **Simulação:** Oportunidade encontrada e seria executada. Lucro simulado: `{lucro_simulado:.4f}%`.")
            self.stats['trades_executados'] += 1
            return
            
        current_amount = volume_a_usar
        
        try:
            # 2. Iterar sobre a rota e executar as ordens
            for i in range(len(cycle_path) - 1):
                coin_from = cycle_path[i]
                coin_to = cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                
                if not pair_id:
                    raise Exception(f"Par inválido na rota: {coin_from}/{coin_to}")
                
                # Recarregar o livro de ofertas para ter dados mais recentes antes de executar
                orderbook = await self.exchange.fetch_order_book(pair_id)
                
                # Usar o melhor preço (top do order book)
                price = orderbook['asks'][0][0] if side == 'buy' else orderbook['bids'][0][0]
                amount = self.exchange.amount_to_precision(pair_id, current_amount / price) if side == 'buy' else self.exchange.amount_to_precision(pair_id, current_amount)
                
                logger.info(f"Criando ordem: {side.upper()} {amount} de {pair_id} @ {price}")
                
                # Executar a ordem de mercado para maior velocidade. Cuidado com o slippage!
                order = await self.exchange.create_order(
                    symbol=pair_id,
                    type='market',
                    side=side,
                    amount=amount,
                )
                
                if order['status'] != 'closed':
                    raise Exception(f"Ordem não foi preenchida: {order['id']}")
                
                # Atualizar o volume para a próxima perna da arbitragem
                if side == 'buy':
                    # A 'amount' de uma ordem de mercado de compra é o valor que você está gastando
                    current_amount = Decimal(str(order['filled'])) * Decimal(str(order['price'])) * (1 - TAXA_TAKER)
                else:
                    # Na venda, o 'filled' é a quantidade de moeda base vendida
                    current_amount = Decimal(str(order['filled'])) * Decimal(str(order['price'])) * (1 - TAXA_TAKER)
            
            # 3. Calcular e registrar o lucro real
            final_amount = current_amount
            lucro_real_percent = ((final_amount - volume_a_usar) / volume_a_usar) * 100
            lucro_real_usdt = final_amount - volume_a_usar
            
            self.stats['trades_executados'] += 1
            self.stats['lucro_total_sessao'] += lucro_real_usdt
            self.bot_data['daily_profit_usdt'] += lucro_real_usdt

            await send_telegram_message(f"✅ **Arbitragem Executada com Sucesso!**\nRota: `{' -> '.join(cycle_path)}`\nVolume: `{volume_a_usar:.2f} USDT`\nLucro: `{lucro_real_usdt:.4f} USDT` (`{lucro_real_percent:.4f}%`)")

        except Exception as e:
            logger.error(f"❌ Falha na execução do trade: {e}", exc_info=True)
            await send_telegram_message(f"❌ **Falha na Execução do Trade:** Algo deu errado na rota `{' -> '.join(cycle_path)}`. Erro: `{e}`")

# --- Funções e Comandos do Telegram ---

async def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem no Telegram: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Envia uma mensagem de boas-vindas."""
    help_text = f"""
👋 **Olá! Sou o Gênesis v17, seu bot de arbitragem.**
Estou monitorando o mercado 24/7 para encontrar oportunidades.
Use /ajuda para ver a lista de comandos.
    """
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Mostra o status atual do bot."""
    dry_run = context.bot_data.get('dry_run', True)
    status_text = "Em operação" if context.bot_data.get('is_running', True) else "Pausado"
    dry_run_text = "Simulação (Dry Run)" if dry_run else "Modo Real"
    
    response = f"""
🤖 **Status do Gênesis v17:**
**Status:** `{status_text}`
**Modo:** `{dry_run_text}`
**Lucro Mínimo:** `{context.bot_data.get('min_profit'):.4f}%`
**Volume de Trade:** `{context.bot_data.get('volume_percent'):.2f}%` do saldo
**Profundidade de Rotas:** `{context.bot_data.get('max_depth')}`
**Stop Loss:** `{context.bot_data.get('stop_loss_usdt') or 'Não definido'}` USDT

**Progresso:** `{context.bot_data.get('progress_status')}`
"""
    await update.message.reply_text(response, parse_mode="Markdown")

async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verifica o saldo da conta."""
    engine = context.bot_data.get('engine')
    if not engine or not engine.exchange:
        await update.message.reply_text("Engine não inicializada. Tente novamente mais tarde.")
        return
    
    try:
        balance = await engine.exchange.fetch_balance()
        saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
        
        response = f"📊 **Saldo OKX:**\n`{saldo_disponivel:.4f} {MOEDA_BASE_OPERACIONAL}` disponível."
        await update.message.reply_text(response, parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao buscar saldo: {e}")

async def modo_real_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ativa o modo de negociação real."""
    context.bot_data['dry_run'] = False
    await update.message.reply_text("✅ **Modo Real Ativado!**\nO bot agora executará ordens de verdade. Use com cautela.")

async def modo_simulacao_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ativa o modo de simulação (dry run)."""
    context.bot_data['dry_run'] = True
    await update.message.reply_text("✅ **Modo Simulação Ativado!**\nO bot apenas simulará trades.")

async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Define o lucro mínimo em porcentagem (ex: /setlucro 0.1)."""
    try:
        min_profit = Decimal(context.args[0])
        if min_profit < 0: raise ValueError
        context.bot_data['min_profit'] = min_profit
        await update.message.reply_text(f"✅ Lucro mínimo definido para `{min_profit:.4f}%`.")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Uso incorreto. Use: `/setlucro <porcentagem>` (ex: `/setlucro 0.1`)")

async def setvolume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Define a porcentagem do saldo a ser usada em cada trade (ex: /setvolume 50)."""
    try:
        volume_percent = Decimal(context.args[0])
        if not (0 < volume_percent <= 100): raise ValueError
        context.bot_data['volume_percent'] = volume_percent
        await update.message.reply_text(f"✅ Volume de trade definido para `{volume_percent:.2f}%` do saldo.")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Uso incorreto. Use: `/setvolume <porcentagem>` (ex: `/setvolume 50`)")

async def pausar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pausa o motor de arbitragem."""
    context.bot_data['is_running'] = False
    await update.message.reply_text("⏸️ Motor de arbitragem pausado.")

async def retomar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Retoma o motor de arbitragem."""
    context.bot_data['is_running'] = True
    await update.message.reply_text("▶️ Motor de arbitragem retomado.")

async def set_stoploss_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Define um stop loss em USDT (ex: /set_stoploss 100). Use 'off' para desativar."""
    try:
        stop_loss_value = context.args[0].lower()
        if stop_loss_value == 'off':
            context.bot_data['stop_loss_usdt'] = None
            await update.message.reply_text("✅ Stop Loss desativado.")
        else:
            stop_loss = Decimal(stop_loss_value)
            if stop_loss <= 0: raise ValueError
            context.bot_data['stop_loss_usdt'] = -stop_loss
            await update.message.reply_text(f"✅ Stop Loss definido para `{stop_loss:.2f} USDT`.")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Uso incorreto. Use: `/set_stoploss <valor>` (ex: `/set_stoploss 100`) ou `/set_stoploss off`.")

async def rotas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra as 5 rotas mais lucrativas simuladas."""
    engine = context.bot_data.get('engine')
    if not engine or not engine.ecg_data:
        await update.message.reply_text("Ainda não há dados de rotas. O primeiro ciclo pode levar alguns minutos.")
        return
    
    top_rotas = "\n".join([
        f"**{i+1}.** `{' -> '.join(r['cycle'])}`\n   Lucro Simulado: `{r['profit']:.4f}%`"
        for i, r in enumerate(engine.ecg_data[:5])
    ])
    
    response = f"📈 **Rotas mais Lucrativas (Simulação):**\n\n{top_rotas}"
    await update.message.reply_text(response, parse_mode="Markdown")

async def ajuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra a lista de comandos disponíveis."""
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
    """Mostra as estatísticas da sessão."""
    engine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("Engine não inicializada.")
        return
    
    stats = engine.stats
    uptime = time.time() - stats['start_time']
    uptime_str = time.strftime("%Hh %Mm %Ss", time.gmtime(uptime))
    
    response = f"""
📊 **Estatísticas da Sessão:**
**Tempo de Atividade:** `{uptime_str}`
**Ciclos de Verificação:** `{stats['ciclos_verificacao_total']}`
**Trades Executados:** `{stats['trades_executados']}`
**Lucro Total (Sessão):** `{stats['lucro_total_sessao']:.4f} {MOEDA_BASE_OPERACIONAL}`
**Erros de Simulação:** `{stats['erros_simulacao']}`
"""
    await update.message.reply_text(response, parse_mode="Markdown")

async def setdepth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Define a profundidade máxima das rotas (ex: /setdepth 4)."""
    engine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("Engine não inicializada.")
        return
    try:
        depth = int(context.args[0])
        if not (MIN_ROUTE_DEPTH <= depth <= 5): # Limite para evitar sobrecarga
            raise ValueError
        context.bot_data['max_depth'] = depth
        await engine.construir_rotas(depth)
        await update.message.reply_text(f"✅ Profundidade máxima das rotas definida para `{depth}`. Rotas recalculadas.")
    except (ValueError, IndexError):
        await update.message.reply_text(f"❌ Uso incorreto. Use: `/setdepth <número>` (min: {MIN_ROUTE_DEPTH}, max: 5)")
        
async def progresso_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mostra o status atual do ciclo de análise."""
    status_text = context.bot_data.get('progress_status', 'Status não disponível.')
    await update.message.reply_text(f"⚙️ **Progresso Atual:**\n`{status_text}`")


async def post_init_tasks(app: Application):
    logger.info("Iniciando motor Gênesis v17 'Antifrágil'...")
    engine = GenesisEngine(app)
    app.bot_data['engine'] = engine
    await send_telegram_message("🤖 *Gênesis v17 'Antifrágil' iniciado.*\nO motor agora é sequencial e resiliente. O primeiro ciclo pode levar alguns minutos.")
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
    
    # Mapeamento de todos os comandos
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
