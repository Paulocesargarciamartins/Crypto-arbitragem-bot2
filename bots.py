# -*- coding: utf-8 -*-
# Gênesis v11.23 - OKX (Versão Final Otimizada)
# O código foi completamente revisado para garantir a sintaxe correta
# da OKX, removendo qualquer vestígio de outros formatos.

import os
import asyncio
import logging
from decimal import Decimal, getcontext, ROUND_DOWN
import time
from datetime import datetime
import json
import traceback

# === IMPORTAÇÃO CCXT ===
try:
    import ccxt
    if not hasattr(ccxt, 'async_support'):
        raise ImportError("ccxt.async_support não encontrado. Verifique a versão instalada.")
except ImportError:
    print("Erro: A biblioteca CCXT ou python-telegram-bot não está instalada. O bot não pode funcionar.")
    ccxt = None

# === IMPORTAÇÃO TELEGRAM ===
try:
    from telegram import Update, Bot
    from telegram.ext import Application, CommandHandler, ContextTypes
except ImportError:
    print("Erro: A biblioteca python-telegram-bot não está instalada.")
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
        """Tenta conectar e carregar os mercados da OKX."""
        if not ccxt:
            logger.critical("CCXT não está disponível. Encerrando.")
            return False
        
        missing_vars = []
        if not OKX_API_KEY:
            missing_vars.append("OKX_API_KEY")
        if not OKX_API_SECRET:
            missing_vars.append("OKX_API_SECRET")
        if not OKX_API_PASSPHRASE:
            missing_vars.append("OKX_API_PASSPHRASE")
            
        if missing_vars:
            error_message = f"❌ Falha crítica: As seguintes chaves de API da OKX estão faltando nas variáveis de ambiente da Heroku: {', '.join(missing_vars)}. Por favor, verifique a tela 'Config Vars' e garanta que os nomes e valores estão corretos."
            logger.critical(error_message)
            await send_telegram_message(error_message)
            return False

        try:
            # === Configuração da OKX ===
            # Verifique se os nomes das chaves de configuração estão corretos.
            # 'apiKey', 'secret' e 'password' são os nomes padrão do CCXT para OKX.
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
            logger.critical("Causa provável: Chave de API, Segredo ou Senha da OKX estão incorretos. Por favor, verifique os valores na Heroku.")
            await self.exchange.close()
            return False
        except Exception as e:
            logger.critical(f"❌ Falha ao conectar com a OKX: {e}")
            logger.critical(f"Tipo de erro: {type(e).__name__}")
            await self.exchange.close()
            return False

    async def construir_rotas(self, max_depth: int):
        """Constroi o grafo de moedas e busca rotas de arbitragem até a profundidade máxima."""
        logger.info(f"Gênesis v11.23: Construindo o mapa de exploração da OKX (Profundidade: {max_depth})...")
        self.graph = {}
        for symbol, market in self.markets.items():
            base, quote = market.get('base'), market.get('quote')
            if not market.get('active'):
                continue
            if not base or not quote:
                continue

            if base not in self.graph: self.graph[base] = []
            if quote not in self.graph: self.graph[quote] = []
            self.graph[base].append(quote)
            self.graph[quote].append(base)

        logger.info(f"Gênesis: Mapa construído com {len(self.graph)} nós. Iniciando busca por rotas de até {max_depth} passos...")
        
        start_node = MOEDA_BASE_OPERACIONAL
        todas_as_rotas = []
        
        def encontrar_ciclos_dfs(u, path, depth):
            if depth > max_depth: return
            for v in self.graph.get(u, []):
                if v == start_node and len(path) > MIN_ROUTE_DEPTH:
                    todas_as_rotas.append(path + [v])
                    continue
                if v not in path:
                    encontrar_ciclos_dfs(v, path + [v], depth + 1)

        encontrar_ciclos_dfs(start_node, [start_node], 1)
        logger.info(f"Gênesis: {len(todas_as_rotas)} rotas brutas encontradas. Aplicando filtro de viabilidade...")
        
        self.rotas_viaveis = {}
        for rota in todas_as_rotas:
            if self._validar_rota_completa(rota):
                self.rotas_viaveis[tuple(rota)] = MINIMO_ABSOLUTO_USDT
        
        self.bot_data['total_rotas'] = len(self.rotas_viaveis)
        logger.info(f"Gênesis: Filtro concluído. {self.bot_data['total_rotas']} rotas serão monitoradas.")

    def _validar_rota_completa(self, cycle_path):
        """
        Valida a rota verificando se todos os pares de moedas existem e estão ativos.
        Retorna True se todos os pares forem viáveis, caso contrário, retorna False.
        """
        try:
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                
                if not pair_id:
                    return False
                
                market = self.markets.get(pair_id)
                if not market or not market.get('active'):
                    return False
            
            return True

        except Exception as e:
            logger.error(f"Erro na validação da rota: {e}", exc_info=True)
            return False

    def _get_pair_details(self, coin_from, coin_to):
        """
        Retorna o par e o lado do trade (buy/sell) para uma conversão,
        utilizando o formato de símbolo correto da OKX (com hífen).
        Este era o ponto de erro.
        """
        pair_buy_side = f"{coin_to}-{coin_from}"
        if pair_buy_side in self.markets:
            return pair_buy_side, 'buy'
        
        pair_sell_side = f"{coin_from}-{coin_to}"
        if pair_sell_side in self.markets:
            return pair_sell_side, 'sell'
            
        return None, None

    async def verificar_oportunidades(self):
        """Loop principal do bot para verificar e executar trades."""
        logger.info("Gênesis: Motor Oportunista (OKX) iniciado.")
        while True:
            await asyncio.sleep(2) 

            # Resetar o lucro diário se o dia mudou
            if datetime.utcnow().day != self.bot_data['last_reset_day']:
                self.bot_data['daily_profit_usdt'] = Decimal('0')
                self.bot_data['last_reset_day'] = datetime.utcnow().day
                await send_telegram_message("📅 **Novo Dia!** O contador de lucro/prejuízo diário foi zerado.")

            if not self.bot_data.get('is_running', True) or self.trade_lock.locked():
                continue
            
            try:
                self.stats['ciclos_verificacao_total'] += 1
                
                balance = await self.exchange.fetch_balance()
                saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
                volume_a_usar = (saldo_disponivel * (self.bot_data['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA

                if volume_a_usar < MINIMO_ABSOLUTO_USDT:
                    await asyncio.sleep(5); continue

                current_tick_results = []
                for cycle_tuple, _ in self.rotas_viaveis.items():
                    cycle_path = list(cycle_tuple)
                    lucro_percentual = await self._simular_trade_com_slippage(cycle_path, volume_a_usar)
                    if lucro_percentual is not None:
                        current_tick_results.append({'cycle': cycle_path, 'profit': lucro_percentual})

                self.ecg_data = sorted(current_tick_results, key=lambda x: x['profit'], reverse=True) if current_tick_results else []
                logger.info(f"Gênesis: Loop de verificação concluído. {len(self.ecg_data)} resultados de ECG gerados.")

                if self.ecg_data and self.ecg_data[0]['profit'] > self.bot_data['min_profit']:
                    async with self.trade_lock:
                        melhor_oportunidade = self.ecg_data[0]
                        logger.info(f"Gênesis: Oportunidade VIÁVEL encontrada ({melhor_oportunidade['profit']:.4f}%).")
                        await self._executar_trade_realista(melhor_oportunidade['cycle'], volume_a_usar)

            except Exception as e:
                logger.error(f"Gênesis: Erro no loop de verificação: {e}", exc_info=True)
                await send_telegram_message(f"⚠️ *Erro no Bot Triangular:* `{type(e).__name__}: {e}`")

    async def _simular_trade_com_slippage(self, cycle_path, volume_inicial):
        """
        Simula o trade na rota usando a liquidez do order book para calcular o lucro real,
        considerando o impacto de mercado (slippage) e as taxas.
        """
        try:
            current_amount = volume_inicial
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                
                if not pair_id or pair_id not in self.markets: 
                    return None
                
                try:
                    orderbook = await self.exchange.fetch_order_book(pair_id)
                except (ccxt.NetworkError, ccxt.ExchangeError) as e:
                    logger.warning(f"Falha ao buscar order book para {pair_id}: {type(e).__name__}: {e}")
                    await send_telegram_message(f"❌ **Erro de API:** Falha ao buscar order book para `{pair_id}`. A rota foi ignorada neste ciclo.")
                    return None
                    
                orders = orderbook['asks'] if side == 'buy' else orderbook['bids']
                
                amount_traded = Decimal('0')
                total_cost = Decimal('0')
                remaining_amount = current_amount
                
                if side == 'buy':
                    for price, size in orders:
                        price = Decimal(str(price))
                        size = Decimal(str(size))
                        
                        cost_of_level = price * size
                        if remaining_amount >= cost_of_level:
                            total_cost += cost_of_level
                            amount_traded += size
                            remaining_amount -= cost_of_level
                        else:
                            size_to_trade = remaining_amount / price
                            total_cost += remaining_amount
                            amount_traded += size_to_trade
                            remaining_amount = Decimal('0')
                            break
                    current_amount = amount_traded * (1 - TAXA_TAKER)
                else: # side == 'sell'
                    for price, size in orders:
                        price = Decimal(str(price))
                        size = Decimal(str(size))
                        
                        if remaining_amount >= size:
                            total_cost += price * size
                            amount_traded += size
                            remaining_amount -= size
                        else:
                            total_cost += price * remaining_amount
                            amount_traded += remaining_amount
                            remaining_amount = Decimal('0')
                            break
                    current_amount = total_cost * (1 - TAXA_TAKER)
                
                if remaining_amount > 0: 
                    return None
            
            lucro_bruto = current_amount - volume_inicial
            lucro_percentual = (lucro_bruto / volume_inicial) * 100 if volume_inicial > 0 else 0
            
            return lucro_percentual
        except Exception as e:
            logger.error(f"Erro na simulação: {e}", exc_info=True)
            await send_telegram_message(f"❌ **Erro na Simulação:** `{type(e).__name__}: {e}`. A rota foi ignorada.")
            return None

    async def _executar_trade_realista(self, cycle_path, volume_a_usar):
        """Executa um trade real na exchange."""
        is_dry_run = self.bot_data.get('dry_run', True)
        
        # Check for daily stop loss limit
        stop_loss_limit = self.bot_data.get('stop_loss_usdt')
        if stop_loss_limit is not None and self.bot_data['daily_profit_usdt'] <= -stop_loss_limit:
            await send_telegram_message(f"🛑 **STOP LOSS ATINGIDO!**\n"
                                        f"Prejuízo diário de **-{stop_loss_limit} USDT** alcançado. O bot foi pausado.")
            self.bot_data['is_running'] = False
            return

        try:
            if is_dry_run:
                await send_telegram_message(f"🎯 **Oportunidade (Simulação)**\n"
                                            f"Rota: `{' -> '.join(cycle_path)}`\n"
                                            f"Lucro Estimado: `{self.ecg_data[0]['profit']:.4f}%`")
                return

            await send_telegram_message(f"**🔴 INICIANDO TRADE REAL**\n"
                                        f"Rota: `{' -> '.join(cycle_path)}`\n"
                                        f"Investimento: `{volume_a_usar:.4f} {cycle_path[0]}`")

            current_amount = volume_a_usar
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                
                params = {}
                amount_to_trade = float(current_amount)
                
                if side == 'buy':
                    params = {'cost': amount_to_trade}
                    amount_to_trade = None 
                
                try:
                    order = await self.exchange.create_market_order(pair_id, side, amount_to_trade, params=params)
                    logger.info(f"Ordem criada: {order['id']}")
                except ccxt.NetworkError as e:
                    await send_telegram_message(f"❌ **FALHA DE REDE NO PASSO {i+1} ({pair_id})**\n"
                                                f"Motivo: `{type(e).__name__}: {e}`\n"
                                                f"ALERTA: Saldo em `{coin_from}` pode estar preso!")
                    return
                except ccxt.ExchangeError as e:
                    await send_telegram_message(f"❌ **FALHA DA EXCHANGE NO PASSO {i+1} ({pair_id})**\n"
                                                f"Motivo: `{type(e).__name__}: {e}`\n"
                                                f"ALERTA: Saldo em `{coin_from}` pode estar preso!")
                    return
                except Exception as e:
                    await send_telegram_message(f"❌ **FALHA CRÍTICA NO PASSO {i+1} ({pair_id})**\n"
                                                f"Motivo: `{type(e).__name__}: {e}`\n"
                                                f"ALERTA: Saldo em `{coin_from}` pode estar preso!")
                    return

                await asyncio.sleep(2) # Pequena pausa para a ordem ser processada

                balance = await self.exchange.fetch_balance()
                saldo_real_da_nova_moeda = Decimal(str(balance.get('free', {}).get(coin_to, '0')))

                if saldo_real_da_nova_moeda == 0:
                    await send_telegram_message(f"❌ **FALHA CRÍTICA:** Saldo de `{coin_to}` é zero após o trade. Abortando.")
                    return
                
                current_amount = saldo_real_da_nova_moeda
                logger.info(f"Passo {i+1} Concluído. Saldo real de {coin_to}: {current_amount}")

            resultado_final = current_amount
            lucro_real = resultado_final - volume_a_usar
            
            # Update daily profit
            self.bot_data['daily_profit_usdt'] += lucro_real
            self.stats['trades_executados'] += 1
            self.stats['lucro_total'] += lucro_real

            await send_telegram_message(f"✅ **Trade Concluído!**\n"
                                        f"Rota: `{' -> '.join(cycle_path)}`\n"
                                        f"Investimento: `{volume_a_usar:.4f} {cycle_path[0]}`\n"
                                        f"Resultado: `{resultado_final:.4f} {cycle_path[-1]}`\n"
                                        f"**Lucro/Prejuízo:** `{lucro_real:.4f} {cycle_path[-1]}`\n"
                                        f"**Lucro Diário:** `{self.bot_data['daily_profit_usdt']:.4f} {cycle_path[0]}`")
        finally:
            logger.info("Ciclo de trade concluído. Aguardando 60s.")
            await asyncio.sleep(60)

async def send_telegram_message(text):
    """Envia uma mensagem para o Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem no Telegram: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Olá! CryptoArbitragemBot v11.23 (OKX) online. Use /status para começar.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("O motor do bot ainda não foi inicializado.")
        return
    bd = context.bot_data
    status_text = "▶️ Rodando" if bd.get('is_running') else "⏸️ Pausado"
    if bd.get('is_running') and engine.trade_lock.locked():
        status_text = "▶️ Rodando (Processando Oportunidade)"
    stop_loss_status = f"`{bd.get('stop_loss_usdt', 'Não definido')}`"
    msg = (f"**📊 Painel de Controle - Gênesis v11.23 (OKX)**\n\n"
           f"**Estado:** `{status_text}`\n"
           f"**Modo:** `{'Simulação' if bd.get('dry_run') else '🔴 REAL'}`\n"
           f"**Lucro Mínimo:** `{bd.get('min_profit')}%`\n"
           f"**Volume por Trade:** `{bd.get('volume_percent')}%`\n"
           f"**Profundidade de Busca:** `{bd.get('max_depth')}`\n"
           f"**Lucro Diário:** `{bd.get('daily_profit_usdt'):.4f} USDT`\n"
           f"**Stop Loss:** `{stop_loss_status}`\n"
           f"**Total de Rotas Monitoradas:** `{bd.get('total_rotas', 0)}`")
    await update.message.reply_text(msg, parse_mode='Markdown')

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("O motor do bot ainda não foi inicializado.")
        return
    
    uptime_seconds = time.time() - engine.stats['start_time']
    m, s = divmod(uptime_seconds, 60)
    h, m = divmod(m, 60)
    uptime_str = f"{int(h)}h {int(m)}m {int(s)}s"
    
    msg = (f"**📈 Estatísticas da Sessão (Gênesis)**\n\n"
           f"**Ativo há:** `{uptime_str}`\n"
           f"**Ciclos de Verificação:** `{engine.stats['ciclos_verificacao_total']}`\n"
           f"**Trades Executados:** `{engine.stats['trades_executados']}`\n"
           f"**Lucro Total da Sessão:** `{engine.stats['lucro_total']:.4f} USDT`\n\n"
           f"**Lucro Diário Atual:** `{context.bot_data.get('daily_profit_usdt', Decimal('0')):.4f} USDT`")
    await update.message.reply_text(msg, parse_mode='Markdown')

async def reset_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("O motor do bot ainda não foi inicializado.")
        return
    
    context.bot_data['daily_profit_usdt'] = Decimal('0')
    engine.stats['trades_executados'] = 0
    engine.stats['lucro_total'] = Decimal('0')
    await update.message.reply_text("✅ **Estatísticas diárias e totais resetadas!**")
    await status_command(update, context)


async def radar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine or not engine.ecg_data or engine.ecg_data[0]['profit'] <= engine.bot_data['min_profit']:
        await update.message.reply_text("🔎 Nenhuma oportunidade de lucro acima do mínimo configurado foi encontrada no momento.\nUse `/radar_all` para ver os resultados da simulação completa.")
        return
    top_5_results = [r for r in engine.ecg_data if r['profit'] > engine.bot_data['min_profit']][:5]
    msg = "📡 **Radar de Oportunidades (Top 5 Rotas Viáveis)**\n\n"
    if not top_5_results:
        await update.message.reply_text("🔎 Nenhuma oportunidade de lucro acima do mínimo configurado foi encontrada no momento.\nUse `/radar_all` para ver os resultados da simulação completa.")
        return
    for result in top_5_results:
        lucro = result['profit']
        emoji = "🔼" if lucro > 0 else "🔽"
        rota_fmt = ' -> '.join(result['cycle'])
        msg += f"**- Rota:** `{rota_fmt}`\n"
        msg += f"  **Resultado Bruto:** `{emoji} {lucro:.4f}%`\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def radar_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine or not engine.ecg_data:
        await update.message.reply_text("⏳ **Aguarde...** O bot está calculando a primeira varredura das rotas. Tente novamente em alguns segundos.")
        return
    top_10_results = engine.ecg_data[:10]
    msg = "📡 **[DIAGNÓSTICO] Radar Completo (Top 10 Rotas Monitoradas)**\n\n"
    if not top_10_results:
        await update.message.reply_text("🔎 Não há rotas para simular no momento. Verifique os logs da Heroku.")
        return
    for result in top_10_results:
        lucro = result['profit']
        emoji = "🔼" if lucro > 0 else "🔽"
        rota_fmt = ' -> '.join(result['cycle'])
        msg += f"**- Rota:** `{rota_fmt}`\n"
        msg += f"  **Resultado Bruto:** `{emoji} {lucro:.4f}%`\n\n"
    await update.message.reply_text(msg, parse_mode='Markdown')

async def debug_radar_loop(context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine: return
    try:
        while True:
            if engine.ecg_data:
                top_10_results = engine.ecg_data[:10]
                msg = "📡 **[DEBUG] Radar Completo (Top 10 Rotas)**\n\n"
                for result in top_10_results:
                    lucro = result['profit']
                    emoji = "🔼" if lucro > 0 else "🔽"
                    rota_fmt = ' -> '.join(result['cycle'])
                    msg += f"**- Rota:** `{rota_fmt}`\n"
                    msg += f"  **Resultado Bruto:** `{emoji} {lucro:.4f}%`\n\n"
                await send_telegram_message(msg)
            else:
                await send_telegram_message("📡 **[DEBUG]** A lista de oportunidades ainda está vazia.")
            await asyncio.sleep(10)
    except asyncio.CancelledError:
        await send_telegram_message("✅ **[DEBUG]** Modo de depuração do radar interrompido.")

async def debug_radar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("O motor do bot ainda não foi inicializado.")
        return
    if context.bot_data.get('debug_radar_task'):
        await update.message.reply_text("O modo de depuração do radar já está ativo.")
        return
    
    task = asyncio.create_task(debug_radar_loop(context))
    context.bot_data['debug_radar_task'] = task
    await update.message.reply_text("✅ **[DEBUG]** Modo de depuração do radar ativado. Enviando relatórios a cada 10 segundos.")

async def stop_debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = context.bot_data.get('debug_radar_task')
    if task:
        task.cancel()
        context.bot_data['debug_radar_task'] = None
    else:
        await update.message.reply_text("O modo de depuração do radar não está ativo.")

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
    except (ccxt.NetworkError, ccxt.ExchangeError) as e:
        await update.message.reply_text(f"❌ Erro ao buscar saldos: `{type(e).__name__}: {e}`")
    except Exception as e:
        await update.message.reply_text(f"❌ Erro genérico ao buscar saldos: `{type(e).__name__}: {e}`")

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
        await update.message.reply_text("⚠️ Uso: `/setlucro 0.005`")

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

async def setdepth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("O motor do bot ainda não foi inicializado.")
        return
    try:
        depth = int(context.args[0])
        if MIN_ROUTE_DEPTH <= depth <= 6:
            context.bot_data['max_depth'] = depth
            await update.message.reply_text(f"✅ Profundidade de busca definida para **{depth}** passos. Reiniciando a busca de rotas...")
            await engine.construir_rotas(depth)
        else:
            await update.message.reply_text(f"⚠️ A profundidade deve ser um número inteiro entre {MIN_ROUTE_DEPTH} e 6.")
    except (IndexError, TypeError, ValueError):
        await update.message.reply_text("⚠️ Uso: `/setdepth 5`")

async def set_stoploss_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        stop_loss_value = Decimal(context.args[0])
        if stop_loss_value > 0:
            context.bot_data['stop_loss_usdt'] = stop_loss_value
            await update.message.reply_text(f"✅ Limite de prejuízo diário definido para **{stop_loss_value:.2f} USDT**.")
        else:
            await update.message.reply_text("⚠️ O valor do stop loss deve ser positivo.")
    except (IndexError, TypeError, ValueError):
        await update.message.reply_text("⚠️ Uso: `/set_stoploss 50.0`")

async def pausar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['is_running'] = False
    await update.message.reply_text("⏸️ **Bot pausado.**")
    await status_command(update, context)

async def retomar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['is_running'] = True
    await update.message.reply_text("✅ **Bot retomado.**")
    await status_command(update, context)

async def post_init_tasks(app: Application):
    logger.info("Bot do Telegram conectado. Iniciando o motor Gênesis para OKX...")
    engine = GenesisEngine(app)
    app.bot_data['engine'] = engine
    
    app.bot_data['dry_run'] = True
    await send_telegram_message("🤖 *CryptoArbitragemBot v11.23 (Otimizado/OKX) iniciado.*\nPor padrão, o bot está em **Modo Simulação**.")

    if await engine.inicializar_exchange():
        await engine.construir_rotas(app.bot_data['max_depth'])
        asyncio.create_task(engine.verificar_oportunidades())
        logger.info("Motor Gênesis (OKX) e tarefas de fundo iniciadas.")
    else:
        await send_telegram_message("❌ **ERRO CRÍTICO:** Não foi possível conectar à OKX. O motor de arbitragem não será iniciado.")

def main():
    if not TELEGRAM_TOKEN:
        logger.critical("❌ O token do Telegram não foi encontrado nas variáveis de ambiente. Verifique se `TELEGRAM_TOKEN` está configurado corretamente na Heroku. Encerrando.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    command_map = {
        "start": start_command, "status": status_command, "stats": stats_command, "reset_stats": reset_stats_command,
        "radar": radar_command, "radar_all": radar_all_command, "debug_radar": debug_radar_command,
        "stop_debug": stop_debug_command, "saldo": saldo_command, 
        "setlucro": setlucro_command, "setvolume": setvolume_command,
        "setdepth": setdepth_command, "set_stoploss": set_stoploss_command,
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
