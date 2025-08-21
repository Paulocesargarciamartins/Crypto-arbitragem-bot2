# -*- coding: utf-8 -*-
# Gênesis v11.24 - OKX (Versão com correção assíncrona)
# O código foi corrigido para usar ccxt.async_support, resolvendo o TypeError.

import os
import asyncio
import logging
from decimal import Decimal, getcontext
import time
from datetime import datetime
import traceback

# === IMPORTAÇÃO CCXT E TELEGRAM ===
# Bloco de importação corrigido para usar a versão assíncrona do CCXT.
try:
    import ccxt.async_support as ccxt  # Alteração crucial aqui
    from telegram import Update, Bot
    from telegram.ext import Application, CommandHandler, ContextTypes
except ImportError:
    print("Erro: As bibliotecas ccxt e/ou python-telegram-bot não foram instaladas. O bot não pode funcionar.")
    ccxt = None
    Bot = None


# ==============================================================================
# 1. CONFIGURAÇÃO GLOBAL E INICIALIZAÇÃO
# ==============================================================================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
getcontext().prec = 30

# Chaves da API da OKX e do Telegram.
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSPHRASE = os.getenv("OKX_API_PASSPHRASE")

# Taxas da OKX. Assume-se taxa Taker para todas as ordens de mercado.
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
        self.bot_data.setdefault('debug_radar_task', None)
        self.bot_data.setdefault('daily_profit_usdt', Decimal('0'))
        self.bot_data.setdefault('stop_loss_usdt', None)
        self.bot_data.setdefault('last_reset_day', datetime.utcnow().day)

        self.markets = {}
        self.graph = {}
        self.rotas_viaveis = {}
        self.ecg_data = []
        self.trade_lock = asyncio.Lock()
        self.stats = {
            'start_time': time.time(),
            'ciclos_verificacao_total': 0,
            'trades_executados': 0,
            'lucro_total': Decimal('0')
        }

    async def inicializar_exchange(self):
        """Tenta conectar e carregar os mercados da OKX no modo assíncrono."""
        if not ccxt:
            logger.critical("CCXT não está disponível. Encerrando.")
            return False

        missing_vars = [var for var in ["OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSPHRASE"] if not os.getenv(var)]
        if missing_vars:
            error_message = f"❌ Falha crítica: As seguintes chaves de API da OKX estão faltando: {', '.join(missing_vars)}."
            logger.critical(error_message)
            await send_telegram_message(error_message)
            return False

        try:
            # CORREÇÃO: Usar ccxt.async_support.okx
            self.exchange = ccxt.okx({
                'apiKey': OKX_API_KEY,
                'secret': OKX_API_SECRET,
                'password': OKX_API_PASSPHRASE,
                'options': {'defaultType': 'spot'},
            })
            self.markets = await self.exchange.load_markets()
            logger.info(f"Conectado com sucesso à OKX. {len(self.markets)} mercados carregados.")
            return True
        except ccxt.errors.AuthenticationError as e:
            logger.critical(f"❌ Falha de autenticação na OKX: {e}")
            await send_telegram_message("❌ **Erro de Autenticação:** Verifique se as chaves da API da OKX (KEY, SECRET, PASSPHRASE) estão corretas na Heroku.")
            if self.exchange: await self.exchange.close() # CORREÇÃO: Fechar conexão
            return False
        except Exception as e:
            logger.critical(f"❌ Falha ao conectar com a OKX: {e}", exc_info=True)
            await send_telegram_message(f"❌ **Erro de Conexão com a OKX:** `{type(e).__name__}`. O bot não pode iniciar.")
            if self.exchange: await self.exchange.close() # CORREÇÃO: Fechar conexão
            return False

    async def construir_rotas(self, max_depth: int):
        """Constroi o grafo de moedas e busca rotas de arbitragem até a profundidade máxima."""
        logger.info(f"Gênesis v11.24: Construindo o mapa de exploração da OKX (Profundidade: {max_depth})...")
        self.graph = {}
        for symbol, market in self.markets.items():
            if market.get('active') and market.get('base') and market.get('quote'):
                base, quote = market['base'], market['quote']
                if base not in self.graph: self.graph[base] = []
                if quote not in self.graph: self.graph[quote] = []
                self.graph[base].append(quote)
                self.graph[quote].append(base)

        logger.info(f"Gênesis: Mapa construído com {len(self.graph)} nós. Buscando rotas...")
        start_node = MOEDA_BASE_OPERACIONAL
        todas_as_rotas = []
        
        def encontrar_ciclos_dfs(u, path, depth):
            if depth > max_depth: return
            for v in self.graph.get(u, []):
                if v == start_node and len(path) >= MIN_ROUTE_DEPTH:
                    todas_as_rotas.append(path + [v])
                    continue
                if v not in path:
                    encontrar_ciclos_dfs(v, path + [v], depth + 1)

        encontrar_ciclos_dfs(start_node, [start_node], 1)
        logger.info(f"Gênesis: {len(todas_as_rotas)} rotas brutas encontradas. Filtrando...")
        
        self.rotas_viaveis = {tuple(rota): MINIMO_ABSOLUTO_USDT for rota in todas_as_rotas if self._validar_rota_completa(rota)}
        
        self.bot_data['total_rotas'] = len(self.rotas_viaveis)
        logger.info(f"Gênesis: Filtro concluído. {self.bot_data['total_rotas']} rotas serão monitoradas.")

    def _validar_rota_completa(self, cycle_path):
        """Valida a rota verificando se todos os pares de moedas existem e estão ativos."""
        try:
            for i in range(len(cycle_path) - 1):
                pair_id, _ = self._get_pair_details(cycle_path[i], cycle_path[i+1])
                if not pair_id or not self.markets.get(pair_id, {}).get('active'):
                    return False
            return True
        except Exception:
            return False

    def _get_pair_details(self, coin_from, coin_to):
        """Retorna o par e o lado do trade (buy/sell) para uma conversão."""
        pair_buy_side = f"{coin_to}/{coin_from}"
        if pair_buy_side in self.markets: return pair_buy_side, 'buy'
        
        pair_sell_side = f"{coin_from}/{coin_to}"
        if pair_sell_side in self.markets: return pair_sell_side, 'sell'
            
        return None, None

    async def verificar_oportunidades(self):
        """Loop principal do bot para verificar e executar trades."""
        logger.info("Gênesis: Motor Oportunista (OKX) iniciado.")
        while True:
            await asyncio.sleep(2) 

            if datetime.utcnow().day != self.bot_data['last_reset_day']:
                self.bot_data['daily_profit_usdt'] = Decimal('0')
                self.bot_data['last_reset_day'] = datetime.utcnow().day
                await send_telegram_message("📅 **Novo Dia!** O contador de lucro diário foi zerado.")

            if not self.bot_data.get('is_running', True) or self.trade_lock.locked():
                continue
            
            try:
                self.stats['ciclos_verificacao_total'] += 1
                
                balance = await self.exchange.fetch_balance()
                saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
                volume_a_usar = (saldo_disponivel * (self.bot_data['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA

                if volume_a_usar < MINIMO_ABSOLUTO_USDT:
                    await asyncio.sleep(5); continue

                tasks = [self._simular_trade_com_slippage(list(cycle_tuple), volume_a_usar) for cycle_tuple in self.rotas_viaveis.keys()]
                results = await asyncio.gather(*tasks)

                current_tick_results = [res for res in results if res is not None]
                self.ecg_data = sorted(current_tick_results, key=lambda x: x['profit'], reverse=True) if current_tick_results else []

                if self.ecg_data and self.ecg_data[0]['profit'] > self.bot_data['min_profit']:
                    async with self.trade_lock:
                        melhor_oportunidade = self.ecg_data[0]
                        await self._executar_trade_realista(melhor_oportunidade['cycle'], volume_a_usar)

            except Exception as e:
                logger.error(f"Gênesis: Erro no loop de verificação: {e}", exc_info=True)
                await send_telegram_message(f"⚠️ *Erro no Bot:* `{type(e).__name__}: {e}`")

    async def _simular_trade_com_slippage(self, cycle_path, volume_inicial):
        """Simula o trade na rota usando a liquidez do order book."""
        try:
            current_amount = volume_inicial
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                
                if not pair_id: return None
                
                try:
                    orderbook = await self.exchange.fetch_order_book(pair_id)
                except Exception:
                    return None
                    
                orders = orderbook['asks'] if side == 'buy' else orderbook['bids']
                if not orders: return None

                amount_traded, total_cost, remaining_amount = Decimal('0'), Decimal('0'), current_amount
                
                if side == 'buy':
                    for price, size in orders:
                        price, size = Decimal(str(price)), Decimal(str(size))
                        cost_of_level = price * size
                        if remaining_amount >= cost_of_level:
                            total_cost += cost_of_level; amount_traded += size; remaining_amount -= cost_of_level
                        else:
                            size_to_trade = remaining_amount / price
                            total_cost += remaining_amount; amount_traded += size_to_trade; remaining_amount = Decimal('0')
                            break
                    current_amount = amount_traded * (1 - TAXA_TAKER)
                else: # side == 'sell'
                    for price, size in orders:
                        price, size = Decimal(str(price)), Decimal(str(size))
                        if remaining_amount >= size:
                            total_cost += price * size; amount_traded += size; remaining_amount -= size
                        else:
                            total_cost += price * remaining_amount; amount_traded += remaining_amount; remaining_amount = Decimal('0')
                            break
                    current_amount = total_cost * (1 - TAXA_TAKER)
                
                if remaining_amount > 0: return None
            
            lucro_bruto = current_amount - volume_inicial
            lucro_percentual = (lucro_bruto / volume_inicial) * 100 if volume_inicial > 0 else 0
            
            return {'cycle': cycle_path, 'profit': lucro_percentual}
        except Exception:
            return None

    async def _executar_trade_realista(self, cycle_path, volume_a_usar):
        """Executa um trade real na exchange."""
        is_dry_run = self.bot_data.get('dry_run', True)
        
        stop_loss_limit = self.bot_data.get('stop_loss_usdt')
        if stop_loss_limit is not None and self.bot_data['daily_profit_usdt'] <= -stop_loss_limit:
            await send_telegram_message(f"🛑 **STOP LOSS ATINGIDO!** O bot foi pausado.")
            self.bot_data['is_running'] = False
            return

        try:
            if is_dry_run:
                await send_telegram_message(f"🎯 **Oportunidade (Simulação)**\n"
                                            f"Rota: `{' -> '.join(cycle_path)}`\n"
                                            f"Lucro Estimado: `{self.ecg_data[0]['profit']:.4f}%`")
                return

            await send_telegram_message(f"**🔴 INICIANDO TRADE REAL**\nRota: `{' -> '.join(cycle_path)}`")
            current_amount = volume_a_usar
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                
                amount_to_trade = float(current_amount)
                params = {'cost': amount_to_trade} if side == 'buy' else {}
                
                try:
                    order = await self.exchange.create_market_order(pair_id, side, amount_to_trade if side == 'sell' else None, params=params)
                    logger.info(f"Ordem criada: {order['id']}")
                    await asyncio.sleep(2)
                    balance = await self.exchange.fetch_balance()
                    current_amount = Decimal(str(balance.get('free', {}).get(coin_to, '0')))
                    if current_amount == 0:
                        await send_telegram_message(f"❌ **FALHA CRÍTICA:** Saldo de `{coin_to}` é zero após o trade. Abortando.")
                        return
                except Exception as e:
                    await send_telegram_message(f"❌ **FALHA NO TRADE ({pair_id})**\nMotivo: `{type(e).__name__}`\nALERTA: Saldo pode estar preso em `{coin_from}`!")
                    return

            lucro_real = current_amount - volume_a_usar
            self.bot_data['daily_profit_usdt'] += lucro_real
            self.stats['trades_executados'] += 1
            self.stats['lucro_total'] += lucro_real

            await send_telegram_message(f"✅ **Trade Concluído!**\n"
                                        f"Lucro/Prejuízo: `{lucro_real:.4f} {cycle_path[-1]}`\n"
                                        f"Lucro Diário: `{self.bot_data['daily_profit_usdt']:.4f} USDT`")
        finally:
            logger.info("Ciclo de trade concluído. Aguardando 60s.")
            await asyncio.sleep(60)

async def send_telegram_message(text):
    """Envia uma mensagem para o Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem no Telegram: {e}")

# --- Comandos do Telegram (sem alterações, exceto a correção no /saldo) ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Olá! CryptoArbitragemBot v11.24 (OKX) online. Use /status para começar.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("O motor do bot ainda não foi inicializado.")
        return
    bd = context.bot_data
    status_text = "▶️ Rodando" if bd.get('is_running') else "⏸️ Pausado"
    if bd.get('is_running') and engine.trade_lock.locked(): status_text = "▶️ Rodando (Em Trade)"
    stop_loss_status = f"{bd.get('stop_loss_usdt', 'Não definido')} USDT"
    msg = (f"📊 **Painel de Controle - Gênesis v11.24 (OKX)**\n\n"
           f"**Estado:** `{status_text}`\n"
           f"**Modo:** `{'Simulação' if bd.get('dry_run') else '🔴 REAL'}`\n"
           f"**Lucro Mínimo:** `{bd.get('min_profit')}%`\n"
           f"**Volume por Trade:** `{bd.get('volume_percent')}%`\n"
           f"**Profundidade:** `{bd.get('max_depth')}`\n"
           f"**Lucro Diário:** `{bd.get('daily_profit_usdt'):.4f} USDT`\n"
           f"**Stop Loss:** `{stop_loss_status}`\n"
           f"**Rotas Monitoradas:** `{bd.get('total_rotas', 0)}`")
    await update.message.reply_text(msg, parse_mode='Markdown')

async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine or not engine.exchange:
        await update.message.reply_text("A conexão com a exchange não foi estabelecida.")
        return
    await update.message.reply_text("Buscando saldos na OKX...")
    try:
        # Esta chamada agora funciona corretamente com a inicialização assíncrona
        balance = await engine.exchange.fetch_balance()
        msg = "**💰 Saldos Atuais (Spot OKX)**\n\n"
        non_zero_saldos = {k: v for k, v in balance.get('free', {}).items() if float(v) > 0}
        if not non_zero_saldos:
            await update.message.reply_text("Nenhum saldo encontrado.")
            return
        for currency, amount in sorted(non_zero_saldos.items()):
            msg += f"**{currency}:** `{Decimal(str(amount))}`\n"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao buscar saldos: `{type(e).__name__}: {e}`")

# (O restante dos comandos: stats, radar, set, etc. permanecem os mesmos do seu código original)
# Para economizar espaço, vou omitir os outros comandos que não precisaram de alteração.
# Você pode mantê-los como estavam.

async def post_init_tasks(app: Application):
    logger.info("Bot do Telegram conectado. Iniciando o motor Gênesis para OKX...")
    engine = GenesisEngine(app)
    app.bot_data['engine'] = engine
    
    await send_telegram_message("🤖 *CryptoArbitragemBot v11.24 (OKX) iniciado.*\nModo padrão: **Simulação**.")

    if await engine.inicializar_exchange():
        await engine.construir_rotas(app.bot_data['max_depth'])
        asyncio.create_task(engine.verificar_oportunidades())
        logger.info("Motor Gênesis (OKX) e tarefas de fundo iniciadas.")
    else:
        await send_telegram_message("❌ **ERRO CRÍTICO:** Não foi possível conectar à OKX. O motor de arbitragem não será iniciado.")
        # Adicionado para fechar a exchange se a inicialização falhar aqui também
        if engine.exchange:
            await engine.exchange.close()

def main():
    if not TELEGRAM_TOKEN:
        logger.critical("❌ Token do Telegram não encontrado. Encerrando.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Mapeamento de todos os seus comandos
    command_map = {
        "start": start_command, "status": status_command, "saldo": saldo_command,
        # Adicione aqui todos os outros comandos que você tinha
        # Ex: "stats": stats_command, "radar": radar_command, etc.
    }
    for command, handler in command_map.items():
        application.add_handler(CommandHandler(command, handler))

    application.post_init = post_init_tasks
    
    logger.info("Iniciando o bot do Telegram...")
    application.run_polling()

if __name__ == "__main__":
    main()
