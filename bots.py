# -*- coding: utf-8 -*-
# Gênesis v17.26 - "Correção de Execução de Trade"
# Adiciona a funcionalidade de uma "saída de emergência" para voltar ao USDT
# se a rota de arbitragem falhar no meio.

import os
import asyncio
import logging
from decimal import Decimal, getcontext
import time
from datetime import datetime
import json
import traceback
from typing import List, Dict, Tuple

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
MIN_PROFIT_DEFAULT = Decimal("0.05")
MOEDA_BASE_OPERACIONAL = 'USDT'
MINIMO_ABSOLUTO_USDT = Decimal("3.1")
MIN_ROUTE_DEPTH = 3
MAX_ROUTE_DEPTH_DEFAULT = 3
MARGEM_PRECO_TAKER = Decimal("1.0001")
BLACKLIST_DURATION_SECONDS = 3600 # 1 hora

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
        
        # Carrega configurações do arquivo
        self.config = self._load_config()
        
        # Configurações do Bot
        self.bot_data.setdefault('is_running', self.config.get('is_running', True))
        self.bot_data.setdefault('min_profit', Decimal(str(self.config.get('min_profit', MIN_PROFIT_DEFAULT))))
        self.bot_data.setdefault('dry_run', self.config.get('dry_run', True))
        self.bot_data.setdefault('volume_percent', Decimal(str(self.config.get('volume_percent', 100.0))))
        self.bot_data.setdefault('max_depth', self.config.get('max_depth', MAX_ROUTE_DEPTH_DEFAULT))
        self.bot_data.setdefault('stop_loss_usdt', Decimal(str(self.config.get('stop_loss_usdt'))) if self.config.get('stop_loss_usdt') is not None else None)
        
        # Dados Operacionais
        self.markets = {}
        self.graph = {}
        self.rotas_viaveis = []
        self.ecg_data = []
        self.current_cycle_results = []
        self.trade_lock = asyncio.Lock()
        self.blacklist = {} # { 'pair_id': timestamp_to_ignore_until }
        
        # Status e Estatísticas
        self.bot_data.setdefault('daily_profit_usdt', Decimal('0'))
        self.bot_data.setdefault('last_reset_day', datetime.utcnow().day)
        self.stats = {'start_time': time.time(), 'ciclos_verificacao_total': 0, 'trades_executados': 0, 'lucro_total_sessao': Decimal('0'), 'erros_simulacao': 0, 'rotas_filtradas': 0}
        self.bot_data['progress_status'] = "Iniciando..."

    def _load_config(self):
        """Carrega as configurações do arquivo JSON. Se não existir, retorna um dicionário vazio."""
        try:
            with open('config.json', 'r') as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            logger.warning("Arquivo 'config.json' não encontrado ou inválido. Usando configurações padrão.")
            return {}

    def save_config(self):
        """Salva as configurações atuais para o arquivo JSON."""
        try:
            with open('config.json', 'w') as f:
                json.dump({
                    "is_running": self.bot_data['is_running'],
                    "min_profit": float(self.bot_data['min_profit']),
                    "dry_run": float(self.bot_data['dry_run']),
                    "volume_percent": float(self.bot_data['volume_percent']),
                    "max_depth": self.bot_data['max_depth'],
                    "stop_loss_usdt": float(self.bot_data['stop_loss_usdt']) if self.bot_data['stop_loss_usdt'] is not None else None
                }, f, indent=2)
            logger.info("Configurações salvas com sucesso.")
        except Exception as e:
            logger.error(f"Erro ao salvar configurações: {e}")

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
        self.rotas_viaveis = []
        
        def encontrar_ciclos_dfs(u, path, depth):
            if depth > max_depth: return
            for v in self.graph.get(u, []):
                if v == MOEDA_BASE_OPERACIONAL and len(path) >= MIN_ROUTE_DEPTH:
                    # Encontrou um ciclo. Verifica se não há moedas duplicadas no meio.
                    rota = path + [v]
                    if len(set(rota[:-1])) == len(rota[:-1]):
                         self.rotas_viaveis.append(tuple(rota))
                elif v not in path:
                    encontrar_ciclos_dfs(v, path + [v], depth + 1)
        
        encontrar_ciclos_dfs(MOEDA_BASE_OPERACIONAL, [MOEDA_BASE_OPERACIONAL], 1)
        
        self.rotas_viaveis = list(set(self.rotas_viaveis))
        self.bot_data['total_rotas'] = len(self.rotas_viaveis)
        await send_telegram_message(f"🗺️ Mapa de rotas reconstruído. {self.bot_data['total_rotas']} rotas cripto-cripto serão monitoradas.")
        self.bot_data['progress_status'] = "Pronto para iniciar ciclos de análise."

    def _get_pair_details(self, coin_from: str, coin_to: str) -> Tuple[str, str, str] | Tuple[None, None, None]:
        """
        Retorna o par (ex: BTC/USDT), o tipo de trade (compra ou venda)
        e a operação (bid ou ask) necessária para a troca de moedas.
        """
        pair_id = f"{coin_to}/{coin_from}"
        if pair_id in self.markets:
            return pair_id, 'buy', 'ask'
        pair_id = f"{coin_from}/{coin_to}"
        if pair_id in self.markets:
            return pair_id, 'sell', 'bid'
            
        return None, None, None
        
    def _is_blacklisted(self, pair_id):
        """Verifica se um par está na lista de bloqueio e, se sim, se já expirou."""
        if pair_id in self.blacklist:
            if time.time() > self.blacklist[pair_id]:
                del self.blacklist[pair_id]
                return False
            return True
        return False
        
    async def verificar_oportunidades(self):
        logger.info("Motor 'Análise de Viabilidade' (v17.26) iniciado.")
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
                
                await send_telegram_message(f"🔎 **Debug:** Saldo de USDT encontrado: `{saldo_disponivel}`")
                
                volume_a_usar = saldo_disponivel * (self.bot_data['volume_percent'] / 100)
                
                if volume_a_usar < MINIMO_ABSOLUTO_USDT:
                    self.bot_data['progress_status'] = f"Volume de trade ({volume_a_usar:.2f} USDT) abaixo do mínimo. Aguardando."
                    await asyncio.sleep(30)
                    continue

                self.current_cycle_results = []
                total_rotas = len(self.rotas_viaveis)
                
                ticker_data = await self.exchange.fetch_tickers()

                for i, cycle_tuple in enumerate(self.rotas_viaveis):
                    self.bot_data['progress_status'] = f"Analisando... Rota {i+1}/{total_rotas}."
                    
                    if any(self._is_blacklisted(f"{cycle_tuple[j]}/{cycle_tuple[j+1]}") or self._is_blacklisted(f"{cycle_tuple[j+1]}/{cycle_tuple[j]}") for j in range(len(cycle_tuple) - 1)):
                        logger.debug(f"Rota {' -> '.join(cycle_tuple)} ignorada (contém par na blacklist).")
                        continue
                        
                    try:
                        lucro_estimado = self._get_route_profitability_estimate(cycle_tuple, ticker_data)
                        if lucro_estimado is None or lucro_estimado < self.bot_data['min_profit']:
                            self.stats['rotas_filtradas'] += 1
                            continue
                            
                        resultado = await self._simular_trade(list(cycle_tuple), volume_a_usar)
                        if resultado:
                            self.current_cycle_results.append(resultado)
                            
                    except Exception as e:
                        self.stats['erros_simulacao'] += 1
                        logger.warning(f"Erro ao simular rota {cycle_tuple}: {e}")
                    
                    await asyncio.sleep(0.1)

                self.ecg_data = sorted(self.current_cycle_results, key=lambda x: x['profit'], reverse=True)
                self.current_cycle_results = []
                logger.info(f"Ciclo de verificação concluído. {len(self.ecg_data)} rotas simuladas com sucesso. {self.stats['erros_simulacao']} erros encontrados e ignorados. {self.stats['rotas_filtradas']} rotas filtradas na pré-análise.")
                self.bot_data['progress_status'] = f"Ciclo concluído. Aguardando próximo ciclo..."

                if self.ecg_data and self.ecg_data[0]['profit'] > self.bot_data['min_profit']:
                    async with self.trade_lock:
                        await self._executar_trade(self.ecg_data[0]['cycle'], volume_a_usar)

            except Exception as e:
                logger.error(f"Erro CRÍTICO no loop de verificação: {e}", exc_info=True)
                await send_telegram_message(f"⚠️ **Erro Grave no Bot:** `{type(e).__name__}`. Verifique os logs.")
                self.bot_data['progress_status'] = f"Erro crítico. Verifique os logs."

    def _get_route_profitability_estimate(self, cycle_path: Tuple, ticker_data: Dict) -> Decimal | None:
        """
        Calcula a lucratividade real da rota rastreando a quantidade de moeda
        após cada conversão.
        """
        current_amount = Decimal('1') 
        initial_currency = cycle_path[0]
        
        for i in range(len(cycle_path) - 1):
            coin_from = cycle_path[i]
            coin_to = cycle_path[i+1]
            
            pair_id, _, price_type = self._get_pair_details(coin_from, coin_to)

            if not pair_id or pair_id not in ticker_data:
                logger.debug(f"Par {pair_id} não encontrado no ticker data.")
                return None
                
            ticker = ticker_data[pair_id]
            price = Decimal(str(ticker.get(price_type)))

            if not price or price == Decimal('0'):
                logger.debug(f"Preço {price_type} inválido para {pair_id}.")
                return None

            if price_type == 'ask':
                current_amount = current_amount / price
            else: # price_type == 'bid'
                current_amount = current_amount * price
            
            current_amount = current_amount * (Decimal('1') - TAXA_TAKER)
        
        final_profit = current_amount - Decimal('1')
        
        if final_profit > 0:
            return (final_profit / Decimal('1')) * 100
        
        return None

    async def _simular_trade(self, cycle_path, volume_inicial):
        """
        Simula o trade em uma rota, consumindo o orderbook para maior precisão.
        """
        current_amount = Decimal(str(volume_inicial))
        
        for i in range(len(cycle_path) - 1):
            coin_from = cycle_path[i]
            coin_to = cycle_path[i+1]
            pair_id, side, _ = self._get_pair_details(coin_from, coin_to)
            
            if not pair_id or self._is_blacklisted(pair_id): return None
            
            orderbook = await self.exchange.fetch_order_book(pair_id)
            orders = orderbook['asks'] if side == 'buy' else orderbook['bids']
            
            if not orders: return None
            
            remaining_amount = current_amount
            final_traded_amount = Decimal('0')
            
            volume_to_consume = remaining_amount if side == 'buy' else remaining_amount
            
            for order in orders:
                if len(order) < 2: continue
                
                price, size, *rest = order
                price, size = Decimal(str(price)), Decimal(str(size))
                
                if side == 'buy':
                    cost_of_order = price * size
                    if volume_to_consume <= cost_of_order:
                        traded_size = volume_to_consume / price
                        final_traded_amount += traded_size
                        remaining_amount = Decimal('0')
                        break
                    else:
                        traded_size = size
                        final_traded_amount += traded_size
                        volume_to_consume -= cost_of_order
                else: # side == 'sell'
                    if volume_to_consume <= size:
                        traded_size = volume_to_consume
                        final_traded_amount += traded_size * price
                        remaining_amount = Decimal('0')
                        break
                    else:
                        traded_size = size
                        final_traded_amount += traded_size * price
                        volume_to_consume -= size
            
            if remaining_amount > 0:
                return None
            
            current_amount = final_traded_amount * (Decimal('1') - TAXA_TAKER)
            
        lucro_percentual = ((current_amount - volume_inicial) / volume_inicial) * 100
        
        if lucro_percentual > 0:
            return {'cycle': cycle_path, 'profit': lucro_percentual}
        
        return None

    async def _executar_saida_de_emergencia(self, asset: str):
        """Tenta vender um ativo específico de volta para a moeda base (USDT)."""
        try:
            balance = await self.exchange.fetch_balance()
            asset_balance = Decimal(str(balance.get('free', {}).get(asset, '0')))
            if asset_balance <= Decimal('0'):
                logger.info(f"Sem saldo de {asset} para saída de emergência.")
                return

            pair_id = f"{asset}/{MOEDA_BASE_OPERACIONAL}"
            
            if pair_id not in self.markets:
                logger.error(f"Par de emergência {pair_id} não encontrado. Não é possível vender o ativo.")
                await send_telegram_message(f"❌ **Falha Crítica:** Não consegui vender `{asset}` de volta para `{MOEDA_BASE_OPERACIONAL}`. Saldo pode estar preso.")
                return

            market = self.exchange.market(pair_id)
            amount_to_sell_str = self.exchange.amount_to_precision(pair_id, asset_balance)
            amount_to_sell = Decimal(amount_to_sell_str)

            logger.warning(f"🚨 Executando SAÍDA DE EMERGÊNCIA: Vendendo {amount_to_sell} de {asset} para {MOEDA_BASE_OPERACIONAL}.")
            
            # Tenta ordem a mercado para garantir a execução
            market_order = await self.exchange.create_order(
                symbol=pair_id,
                type='market',
                side='sell',
                amount=amount_to_sell
            )

            await send_telegram_message(f"⚠️ **Saída de Emergência:** Ordem de venda de `{amount_to_sell:.4f} {asset}` foi enviada para o mercado. Isso pode resultar em um pequeno prejuízo para liberar o saldo.")

        except Exception as e:
            logger.error(f"❌ Erro na saída de emergência para {asset}: {e}", exc_info=True)
            await send_telegram_message(f"❌ **Saída de Emergência Falhou:** Não foi possível vender `{asset}`. Erro: `{e}`")


    async def _executar_trade(self, cycle_path, volume_a_usar):
        logger.info(f"🚀 Oportunidade encontrada. Executando rota: {' -> '.join(cycle_path)}.")
        
        if self.bot_data['dry_run']:
            lucro_simulado = self.ecg_data[0]['profit']
            await send_telegram_message(f"✅ **Simulação:** Oportunidade encontrada e seria executada. Lucro simulado: `{lucro_simulado:.4f}%`.")
            self.stats['trades_executados'] += 1
            return
            
        current_amount_asset = Decimal(str(volume_a_usar))
        
        for i in range(len(cycle_path) - 1):
            coin_from = cycle_path[i]
            coin_to = cycle_path[i+1]
            pair_id, side, _ = self._get_pair_details(coin_from, coin_to)
            
            try:
                if not pair_id:
                    raise Exception(f"Par inválido na rota: {coin_from}/{coin_to}")
                
                if self._is_blacklisted(pair_id):
                    raise ValueError(f"Par {pair_id} está na lista de bloqueio temporária. Ignorando a execução.")

                orderbook = await self.exchange.fetch_order_book(pair_id)
                if not orderbook['asks'] or not orderbook['bids']:
                    raise ValueError(f"Orderbook vazio para o par {pair_id}.")
                
                market = self.exchange.market(pair_id)
                
                if side == 'sell':
                    limit_price = Decimal(str(orderbook['bids'][0][0])) / MARGEM_PRECO_TAKER
                    raw_amount_to_trade = current_amount_asset
                else: # side == 'buy'
                    limit_price = Decimal(str(orderbook['asks'][0][0])) * MARGEM_PRECO_TAKER
                    raw_amount_to_trade = current_amount_asset / limit_price
                
                amount_to_trade_str = self.exchange.amount_to_precision(pair_id, raw_amount_to_trade)
                amount_to_trade = Decimal(str(amount_to_trade_str))

                notional_value = amount_to_trade * limit_price
                
                min_amount = Decimal(str(market['limits']['amount']['min']))
                min_notional_market = Decimal(str(market['limits'].get('notional', {}).get('min', '0')))
                
                if amount_to_trade < min_amount:
                    raise ValueError(f"Volume calculado `{amount_to_trade}` é muito baixo para o par `{pair_id}` (mínimo: {min_amount}).")
                
                if notional_value < min_notional_market:
                    raise ValueError(f"Valor nocional ({notional_value:.2f} {market['quote']}) é inferior ao mínimo ({min_notional_market}) da OKX para o par `{pair_id}`.")

                logger.info(f"Tentando ordem LIMIT: {side.upper()} {amount_to_trade} de {pair_id} @ {limit_price}")

                limit_order = await self.exchange.create_order(
                    symbol=pair_id,
                    type='limit',
                    side=side,
                    amount=amount_to_trade,
                    price=limit_price,
                )
                
                await asyncio.sleep(3) 
                
                order_status = await self.exchange.fetch_order(limit_order['id'], pair_id)
                
                if order_status['status'] == 'closed':
                    logger.info(f"✅ Ordem LIMIT preenchida com sucesso!")
                else:
                    logger.warning(f"❌ Ordem LIMIT não preenchida. Tentando cancelar e usar ordem a MERCADO.")
                    
                    try:
                        await self.exchange.cancel_order(limit_order['id'], pair_id)
                    except ccxt.ExchangeError as e:
                        if '51400' in str(e):
                            logger.info("✅ Confirmação: Ordem preenchida em um 'race condition'. Prosseguindo.")
                            order_status = await self.exchange.fetch_order(limit_order['id'], pair_id)
                        else:
                            raise e
                    else:
                        market_order = await self.exchange.create_order(
                            symbol=pair_id,
                            type='market',
                            side=side,
                            amount=amount_to_trade
                        )
                        order_status = await self.exchange.fetch_order(market_order['id'], pair_id)
                        if order_status['status'] != 'closed':
                            raise Exception(f"Ordem de MERCADO não preenchida: {order_status['id']}")
                        logger.info(f"✅ Ordem a MERCADO preenchida com sucesso!")

                filled_amount_raw = order_status.get('filled', '0')
                filled_price_raw = order_status.get('price', '0')

                if filled_amount_raw is None or filled_price_raw is None:
                    raise ValueError("Dados preenchidos inválidos da ordem.")

                filled_amount = Decimal(str(filled_amount_raw))
                filled_price = Decimal(str(filled_price_raw))
                
                if side == 'buy':
                    fee_amount = filled_amount * TAXA_TAKER
                    current_amount_asset = filled_amount - fee_amount
                else:
                    fee_amount = filled_amount * filled_price * TAXA_TAKER
                    current_amount_asset = (filled_amount * filled_price) - fee_amount
            
            except Exception as e:
                logger.error(f"❌ Falha ao executar trade de {coin_from} para {coin_to}: {e}", exc_info=True)
                await send_telegram_message(f"❌ **Falha na Execução do Trade:** Algo deu errado na rota `{coin_from} -> {coin_to}`. Erro: `{e}`")
                
                # Se a falha ocorreu no meio do caminho, tentamos uma saída de emergência
                if i > 0:
                    asset_to_dump = cycle_path[i]
                    await self._executar_saida_de_emergencia(asset_to_dump)
                
                self.blacklist[pair_id] = time.time() + BLACKLIST_DURATION_SECONDS
                return # Interrompe a execução da rota atual

        # Se o loop de trades foi concluído com sucesso
        final_amount = current_amount_asset
        lucro_real_percent = ((final_amount - volume_a_usar) / volume_a_usar) * 100
        lucro_real_usdt = final_amount - volume_a_usar
        
        self.stats['trades_executados'] += 1
        self.stats['lucro_total_sessao'] += lucro_real_usdt
        self.bot_data['daily_profit_usdt'] += lucro_real_usdt
        self.save_config()

        await send_telegram_message(f"✅ **Arbitragem Executada com Sucesso!**\nRota: `{' -> '.join(cycle_path)}`\nVolume: `{volume_a_usar:.4f} USDT`\nLucro: `{lucro_real_usdt:.4f} USDT` (`{lucro_real_percent:.4f}%`)")


async def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem no Telegram: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = f"""
👋 **Olá! Sou o Gênesis v17.26, seu bot de arbitragem.**
Estou monitorando o mercado 24/7 para encontrar oportunidades.
Use /ajuda para ver a lista de comandos.
    """
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    dry_run = context.bot_data.get('dry_run', True)
    status_text = "Em operação" if context.bot_data.get('is_running', True) else "Pausado"
    dry_run_text = "Simulação (Dry Run)" if dry_run else "Modo Real"
    
    response = f"""
🤖 **Status do Gênesis v17.26:**
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
    context.bot_data['dry_run'] = False
    context.bot_data['engine'].save_config()
    await update.message.reply_text("✅ **Modo Real Ativado!**\nO bot agora executará ordens de verdade. Use com cautela.")

async def modo_simulacao_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['dry_run'] = True
    context.bot_data['engine'].save_config()
    await update.message.reply_text("✅ **Modo Simulação Ativado!**\nO bot apenas simulará trades.")

async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        min_profit = Decimal(context.args[0])
        if min_profit < 0: raise ValueError
        context.bot_data['min_profit'] = min_profit
        context.bot_data['engine'].save_config()
        await update.message.reply_text(f"✅ Lucro mínimo definido para `{min_profit:.4f}%`.")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Uso incorreto. Use: `/setlucro <porcentagem>` (ex: `/setlucro 0.1`)")

async def setvolume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        volume_percent = Decimal(context.args[0])
        if not (0 < volume_percent <= 100): raise ValueError
        context.bot_data['volume_percent'] = volume_percent
        context.bot_data['engine'].save_config()
        await update.message.reply_text(f"✅ Volume de trade definido para `{volume_percent:.2f}%` do saldo.")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Uso incorreto. Use: `/setvolume <porcentagem>` (ex: `/setvolume 50`)")

async def pausar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['is_running'] = False
    context.bot_data['engine'].save_config()
    await update.message.reply_text("⏸️ Motor de arbitragem pausado.")

async def retomar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['is_running'] = True
    context.bot_data['engine'].save_config()
    await update.message.reply_text("▶️ Motor de arbitragem retomado.")

async def set_stoploss_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        stop_loss_value = context.args[0].lower()
        if stop_loss_value == 'off':
            context.bot_data['stop_loss_usdt'] = None
            await update.message.reply_text("✅ Stop Loss desativado.")
        else:
            stop_loss = Decimal(stop_loss_value)
            if stop_loss <= 0: raise ValueError
            context.bot_data['stop_loss_usdt'] = -stop_loss
            context.bot_data['engine'].save_config()
            await update.message.reply_text(f"✅ Stop Loss definido para `{stop_loss:.2f} USDT`.")
    except (ValueError, IndexError):
        await update.message.reply_text("❌ Uso incorreto. Use: `/set_stoploss <valor>` (ex: `/set_stoploss 100`) ou `/set_stoploss off`.")

async def rotas_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine = context.bot_data.get('engine')
    if engine and engine.ecg_data:
        top_rotas = "\n".join([
            f"**{i+1}.** `{' -> '.join(r['cycle'])}`\n   Lucro Simulado: `{r['profit']:.4f}%`"
            for i, r in enumerate(engine.ecg_data[:5])
        ])
        response = f"📈 **Rotas mais Lucrativas (Simulação):**\n\n{top_rotas}"
        await update.message.reply_text(response, parse_mode="Markdown")
        return
    await update.message.reply_text("Ainda não há dados de rotas. O bot pode estar em um ciclo inicial de análise.")

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
**Rotas Filtradas:** `{stats['rotas_filtradas']}`
**Trades Executados:** `{stats['trades_executados']}`
**Lucro Total (Sessão):** `{stats['lucro_total_sessao']:.4f} {MOEDA_BASE_OPERACIONAL}`
**Erros de Simulação:** `{stats['erros_simulacao']}`
"""
    await update.message.reply_text(response, parse_mode="Markdown")

async def setdepth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("Engine não inicializada.")
        return
    try:
        depth = int(context.args[0])
        if not (MIN_ROUTE_DEPTH <= depth <= 5):
            raise ValueError
        context.bot_data['max_depth'] = depth
        await engine.construir_rotas(depth)
        context.bot_data['engine'].save_config()
        await update.message.reply_text(f"✅ Profundidade máxima das rotas definida para `{depth}`. Rotas recalculadas.")
    except (ValueError, IndexError):
        await update.message.reply_text(f"❌ Uso incorreto. Use: `/setdepth <número>` (min: {MIN_ROUTE_DEPTH}, max: 5)")
        
async def progresso_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status_text = context.bot_data.get('progress_status', 'Status não disponível.')
    await update.message.reply_text(f"⚙️ **Progresso Atual:**\n`{status_text}`")

async def post_init_tasks(app: Application):
    logger.info("Iniciando motor Gênesis v17.26 'Correção de Execução de Trade'...")
    engine = GenesisEngine(app)
    app.bot_data['engine'] = engine
    await send_telegram_message("🤖 *Gênesis v17.26 'Correção de Execução de Trade' iniciado.*\nAs configurações agora são salvas e carregadas automaticamente.")
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
