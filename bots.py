# -*- coding: utf-8 -*-
# CryptoArbitragemBot v10.1 - O Híbrido (Corrigido)
# Corrigido o erro de importação 'NameError' para a classe 'Bot' do Telegram.
# Revisado para garantir a estabilidade na inicialização.

import os
import asyncio
import logging
from decimal import Decimal, getcontext
import time
import uuid
import json

try:
    import ccxt.async_support as ccxt
except ImportError:
    print("Erro: A biblioteca CCXT não está instalada. O bot não pode funcionar.")
    ccxt = None

# IMPORTAÇÃO CORRIGIDA: 'Bot' foi adicionado aqui.
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# ==============================================================================
# 1. CONFIGURAÇÃO GLOBAL E INICIALIZAÇÃO
# ==============================================================================
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
getcontext().prec = 30

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_API_SECRET = os.getenv("OKX_API_SECRET", "")
OKX_API_PASSPHRASE = os.getenv("OKX_API_PASSPHRASE", "")

TAXA_MAKER = Decimal("0.001")
MIN_PROFIT_DEFAULT = Decimal("0.2")
MARGEM_DE_SEGURANCA = Decimal("0.995")
MOEDA_BASE_OPERACIONAL = 'USDT'
MINIMO_ABSOLUTO_USDT = Decimal("3.1")
MAX_ROUTE_DEPTH = 5

# ==============================================================================
# 2. GÊNESIS ENGINE v10.1 (ADAPTADO PARA CCXT/OKX)
# ==============================================================================
class GenesisEngine:
    # ... (O corpo da classe GenesisEngine permanece exatamente o mesmo da v10)
    def __init__(self, application: Application):
        self.app = application
        self.bot_data = application.bot_data
        self.exchange = None

        self.bot_data.setdefault('is_running', True)
        self.bot_data.setdefault('min_profit', MIN_PROFIT_DEFAULT)
        self.bot_data.setdefault('dry_run', True)
        self.bot_data.setdefault('volume_percent', Decimal("100.0"))
        
        self.prices = {}
        self.markets = {}
        self.graph = {}
        self.rotas_viaveis = {}
        self.ecg_data = []
        self.trade_lock = asyncio.Lock()

    async def inicializar_exchange(self):
        if not ccxt:
            logger.critical("CCXT não está disponível. Encerrando.")
            return False
        
        if not all([OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE]):
            logger.critical("As chaves de API da OKX não estão configuradas. Encerrando.")
            return False

        try:
            self.exchange = ccxt.okx({
                'apiKey': OKX_API_KEY,
                'secret': OKX_API_SECRET,
                'password': OKX_API_PASSPHRASE,
                'options': {'defaultType': 'spot'}
            })
            self.markets = await self.exchange.load_markets()
            logger.info(f"Conectado com sucesso à OKX. {len(self.markets)} mercados carregados.")
            return True
        except Exception as e:
            logger.critical(f"Falha ao conectar com a OKX: {e}")
            if self.exchange:
                await self.exchange.close()
            return False

    async def construir_rotas(self):
        logger.info("Gênesis v10: Construindo o mapa de exploração da OKX...")
        for symbol, market in self.markets.items():
            if market.get('active') and market.get('quote') and market.get('base'):
                base, quote = market['base'], market['quote']
                if base not in self.graph: self.graph[base] = []
                if quote not in self.graph: self.graph[quote] = []
                self.graph[base].append(quote)
                self.graph[quote].append(base)

        logger.info(f"Gênesis: Mapa construído. Iniciando busca por rotas de até {MAX_ROUTE_DEPTH} passos...")
        start_node = MOEDA_BASE_OPERACIONAL
        todas_as_rotas = []
        
        def encontrar_ciclos_dfs(u, path, depth):
            if depth > MAX_ROUTE_DEPTH: return
            for v in self.graph.get(u, []):
                if v == start_node and len(path) > 2:
                    todas_as_rotas.append(path + [v])
                    continue
                if v not in path:
                    encontrar_ciclos_dfs(v, path + [v], depth + 1)

        encontrar_ciclos_dfs(start_node, [start_node], 1)
        logger.info(f"Gênesis: {len(todas_as_rotas)} rotas brutas encontradas. Aplicando filtro de viabilidade...")

        for rota in todas_as_rotas:
            custo_minimo = self._calcular_custo_minimo_rota(rota)
            if custo_minimo is not None and custo_minimo > 0:
                self.rotas_viaveis[tuple(rota)] = custo_minimo
        
        total_rotas_viaveis = len(self.rotas_viaveis)
        logger.info(f"Gênesis: Filtro concluído. {total_rotas_viaveis} rotas serão monitoradas.")
        self.bot_data['total_ciclos'] = total_rotas_viaveis

    def _get_pair_details(self, coin_from, coin_to):
        pair_buy_side = f"{coin_to}/{coin_from}"
        if pair_buy_side in self.markets:
            return pair_buy_side, 'buy'
        
        pair_sell_side = f"{coin_from}/{coin_to}"
        if pair_sell_side in self.markets:
            return pair_sell_side, 'sell'
            
        return None, None

    def _calcular_custo_minimo_rota(self, cycle_path):
        try:
            custo_minimo_final = Decimal('0')
            for i in range(len(cycle_path) - 2, -1, -1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                if not pair_id: return None
                
                market = self.markets.get(pair_id)
                if not market or not market.get('limits', {}).get('cost', {}).get('min'):
                    continue

                min_cost = Decimal(str(market['limits']['cost']['min']))
                
                if side == 'buy':
                    custo_minimo_final = max(custo_minimo_final, min_cost)
                else:
                    custo_minimo_final = max(custo_minimo_final, min_cost)
            return custo_minimo_final
        except Exception:
            return None

    async def verificar_oportunidades(self):
        logger.info("Gênesis: Motor Oportunista (OKX) iniciado.")
        while True:
            if not self.bot_data.get('is_running', True) or self.trade_lock.locked():
                await asyncio.sleep(1); continue
            try:
                tickers = await self.exchange.fetch_tickers()
                self.prices = {symbol: {'ask': Decimal(str(ticker['ask'])), 'bid': Decimal(str(ticker['bid']))} for symbol, ticker in tickers.items() if ticker.get('ask') and ticker.get('bid')}
                
                balance = await self.exchange.fetch_balance()
                saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
                volume_a_usar = (saldo_disponivel * (self.bot_data['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA

                if volume_a_usar < MINIMO_ABSOLUTO_USDT:
                    await asyncio.sleep(5); continue

                current_tick_results = []
                for cycle_tuple, custo_minimo in self.rotas_viaveis.items():
                    if volume_a_usar < custo_minimo: continue
                    
                    cycle_path = list(cycle_tuple)
                    lucro_percentual, _ = self._calcular_lucro_executavel(cycle_path, volume_a_usar)
                    if lucro_percentual is not None:
                        current_tick_results.append({'cycle': cycle_path, 'profit': lucro_percentual})
                
                self.ecg_data = sorted(current_tick_results, key=lambda x: x['profit'], reverse=True) if current_tick_results else []

                if self.ecg_data and self.ecg_data[0]['profit'] > self.bot_data['min_profit']:
                    async with self.trade_lock:
                        melhor_oportunidade = self.ecg_data[0]
                        logger.info(f"Gênesis: Oportunidade VIÁVEL encontrada ({melhor_oportunidade['profit']:.4f}%).")
                        await self._executar_trade_realista(melhor_oportunidade['cycle'], volume_a_usar)

            except Exception as e:
                logger.error(f"Gênesis: Erro no loop de verificação: {e}", exc_info=True)
                await send_telegram_message(f"⚠️ *Erro no Bot Triangular:* `{e}`")
                await asyncio.sleep(10)
            await asyncio.sleep(3)

    def _calcular_lucro_executavel(self, cycle_path, volume_inicial):
        try:
            current_amount = volume_inicial
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                if not pair_id or pair_id not in self.prices: return None, None
                
                market = self.markets.get(pair_id)
                limits = market.get('limits', {})
                min_cost = Decimal(str(limits.get('cost', {}).get('min', '0')))
                min_amount = Decimal(str(limits.get('amount', {}).get('min', '0')))
                
                price = self.prices[pair_id]['ask'] if side == 'buy' else self.prices[pair_id]['bid']
                if price == 0: return None, None

                if side == 'buy':
                    if current_amount < min_cost: return None, None
                    current_amount = (current_amount / price)
                else:
                    if current_amount < min_amount: return None, None
                    current_amount = (current_amount * price)
                
                current_amount *= (1 - TAXA_MAKER)

            lucro_bruto = current_amount - volume_inicial
            lucro_percentual = (lucro_bruto / volume_inicial) * 100 if volume_inicial > 0 else 0
            return lucro_percentual, current_amount
        except Exception:
            return None, None

    async def _executar_trade_realista(self, cycle_path, volume_a_usar):
        is_dry_run = self.bot_data.get('dry_run', True)
        try:
            if is_dry_run:
                await send_telegram_message(f"🎯 **Oportunidade (Simulação)**\n"
                                            f"`{' -> '.join(cycle_path)}`\n"
                                            f"Lucro Estimado: `{self.ecg_data[0]['profit']:.4f}%`")
                return

            logger.info(f"Iniciando Trade REAL: {' -> '.join(cycle_path)} com {volume_a_usar:.4f} {cycle_path[0]}")

            current_amount = volume_a_usar
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                
                params = {}
                amount_to_trade = float(current_amount)
                
                if side == 'buy':
                    params = {'cost': amount_to_trade}
                    amount_to_trade = None 
                
                logger.info(f"CRIANDO ORDEM PASSO {i+1}: Par={pair_id}, Lado={side}, Quantidade={amount_to_trade}, Custo={params.get('cost')}")
                
                try:
                    await self.exchange.create_market_order(pair_id, side, amount_to_trade, params=params)
                except Exception as e:
                    await send_telegram_message(f"❌ **FALHA NO PASSO {i+1} ({pair_id})**\n**Motivo:** `{e}`\n**ALERTA:** Saldo em `{coin_from}` pode estar preso!")
                    return
                
                await asyncio.sleep(2)

                balance = await self.exchange.fetch_balance()
                saldo_real_da_nova_moeda = Decimal(str(balance.get('free', {}).get(coin_to, '0')))

                if saldo_real_da_nova_moeda == 0:
                    await send_telegram_message(f"❌ **FALHA CRÍTICA:** Saldo de `{coin_to}` é zero após o trade. Abortando.")
                    return
                
                current_amount = saldo_real_da_nova_moeda
                logger.info(f"Passo {i+1} Concluído. Saldo real de {coin_to}: {current_amount}")

            resultado_final = current_amount
            lucro_real = resultado_final - volume_a_usar
            await send_telegram_message(f"✅ **Trade Concluído!**\n"
                                        f"`{' -> '.join(cycle_path)}`\n"
                                        f"Investimento: `{volume_a_usar:.4f} {cycle_path[0]}`\n"
                                        f"Resultado: `{resultado_final:.4f} {cycle_path[-1]}`\n"
                                        f"**Lucro/Prejuízo:** `{lucro_real:.4f} {cycle_path[-1]}`")
        finally:
            logger.info("Ciclo de trade concluído. Aguardando 60s.")
            await asyncio.sleep(60)

# ==============================================================================
# 3. LÓGICA DO TELEGRAM BOT (COMMAND HANDLERS)
# ==============================================================================
async def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem no Telegram: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Olá! CryptoArbitragemBot v10.1 (Híbrido/OKX) online. Use /status para começar.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("O motor do bot ainda não foi inicializado.")
        return
    bd = context.bot_data
    status_text = "▶️ Rodando" if bd.get('is_running') else "⏸️ Pausado"
    if bd.get('is_running') and engine.trade_lock.locked():
        status_text = "▶️ Rodando (Processando Oportunidade)"
    msg = (f"**📊 Painel de Controle - Gênesis v10.1 (OKX)**\n\n"
           f"**Estado:** `{status_text}`\n"
           f"**Modo:** `{'Simulação' if bd.get('dry_run') else '🔴 REAL'}`\n"
           f"**Lucro Mínimo:** `{bd.get('min_profit')}%`\n"
           f"**Volume por Trade:** `{bd.get('volume_percent')}%`\n"
           f"**Total de Rotas Monitoradas:** `{bd.get('total_ciclos', 0)}`")
    await update.message.reply_text(msg, parse_mode='Markdown')

async def radar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine or not engine.ecg_data:
        await update.message.reply_text("📡 Eletrocardiograma ainda calculando ou nenhuma oportunidade viável encontrada.")
        return
    top_5_results = engine.ecg_data[:5]
    msg = "📡 **Radar de Oportunidades (Top 5 Rotas Viáveis)**\n\n"
    for result in top_5_results:
        lucro = result['profit']
        emoji = "🔼" if lucro > 0 else "🔽"
        rota_fmt = ' -> '.join(result['cycle'])
        msg += f"**- Rota:** `{rota_fmt}`\n"
        msg += f"  **Resultado Bruto:** `{emoji} {lucro:.4f}%`\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine or not engine.exchange:
        await update.message.reply_text("A conexão com a exchange ainda não foi estabelecida.")
        return
    await update.message.reply_text("Buscando saldos na OKX...")
    try:
        balance = await engine.exchange.fetch_balance()
        msg = "**💰 Saldos Atuais (Spot OKX)**\n\n"
        non_zero_saldos = {k: v for k, v in balance.get('free', {}).items() if float(v) > 0}
        if not non_zero_saldos:
            await update.message.reply_text("Nenhum saldo encontrado.")
            return
        for currency, amount in non_zero_saldos.items():
            msg += f"**{currency}:** `{Decimal(str(amount))}`\n"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao buscar saldos: `{e}`")

async def modo_real_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['dry_run'] = False
    await update.message.reply_text("🔴 **MODO REAL ATIVADO.** O bot agora executará trades reais na OKX.")
    await status_command(update, context)

async def modo_simulacao_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['dry_run'] = True
    await update.message.reply_text("🔵 **Modo Simulação Ativado.**")
    await status_command(update, context)

async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.bot_data['min_profit'] = Decimal(context.args[0])
        await update.message.reply_text(f"✅ Lucro mínimo alvo definido para **{context.args[0]}%**.")
    except (IndexError, TypeError, ValueError):
        await update.message.reply_text("⚠️ Uso: `/setlucro 0.2`")

async def setvolume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        volume_str = context.args[0].replace('%', '').strip()
        volume = Decimal(volume_str)
        if 0 < volume <= 100:
            context.bot_data['volume_percent'] = volume
            await update.message.reply_text(f"✅ Volume por trade definido para **{volume}%** do saldo.")
        else:
            await update.message.reply_text("⚠️ O volume deve ser entre 1 e 100.")
    except (IndexError, TypeError, ValueError):
        await update.message.reply_text("⚠️ Uso: `/setvolume 100`")

async def pausar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['is_running'] = False
    await update.message.reply_text("⏸️ **Bot pausado.**")
    await status_command(update, context)

async def retomar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['is_running'] = True
    await update.message.reply_text("✅ **Bot retomado.**")
    await status_command(update, context)

# ==============================================================================
# 4. INICIALIZAÇÃO E EXECUÇÃO PRINCIPAL
# ==============================================================================
async def post_init_tasks(app: Application):
    logger.info("Bot do Telegram conectado. Iniciando o motor Gênesis para OKX...")
    engine = GenesisEngine(app)
    app.bot_data['engine'] = engine
    
    app.bot_data['dry_run'] = True
    await send_telegram_message("🤖 *CryptoArbitragemBot v10.1 (Híbrido/OKX) iniciado.*\nPor padrão, o bot está em **Modo Simulação**.")

    if await engine.inicializar_exchange():
        await engine.construir_rotas()
        asyncio.create_task(engine.verificar_oportunidades())
        logger.info("Motor Gênesis (OKX) e tarefas de fundo iniciadas.")
    else:
        await send_telegram_message("❌ **ERRO CRÍTICO:** Não foi possível conectar à OKX. O motor de arbitragem não será iniciado.")

def main():
    if not TELEGRAM_TOKEN:
        logger.critical("O token do Telegram não foi encontrado. Encerrando.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    command_map = {
        "start": start_command, "status": status_command, "radar": radar_command,
        "saldo": saldo_command, "setlucro": setlucro_command, "setvolume": setvolume_command,
        "modo_real": modo_real_command, "modo_simulacao": modo_simulacao_command,
        "pausar": pausar_command, "retomar": retomar_command,
    }
    for command, handler in command_map.items():
        application.add_handler(CommandHandler(command, handler))

    application.post_init = post_init_tasks
    
    logger.info("Iniciando o bot do Telegram...")
    application.run_polling()

if __name__ == "__main__":
    main()
