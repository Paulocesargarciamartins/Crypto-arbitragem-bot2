# -*- coding: utf-8 -*-

Gênesis v17.40 - Correções aplicadas

Alterações principais:

- Evita adicionar chave None à blacklist

- Validação robusta do "notional" mínimo (vários formatos possíveis)

- Re-fetch do pedido após cancelamento para obter remaining preciso

- Correção no /set_stoploss para salvar corretamente e usar valor positivo

- Aplicação básica do stop-loss (pausa o bot se prejuízo diário exceder)

- Salvamento consistente do config.json após mudanças

import os import asyncio import logging from decimal import Decimal, getcontext import time from datetime import datetime import json import traceback from typing import List, Dict, Tuple

=== IMPORTAÇÃO CCXT E TELEGRAM ===

try: import ccxt.async_support as ccxt from telegram import Update, Bot from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters except ImportError: print("Erro: Bibliotecas essenciais não instaladas.") ccxt = None Bot = None

==============================================================================

1. CONFIGURAÇÃO GLOBAL E INICIALIZAÇÃO

==============================================================================

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO) logger = logging.getLogger(name) getcontext().prec = 30

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") OKX_API_KEY = os.getenv("OKX_API_KEY") OKX_API_SECRET = os.getenv("OKX_API_SECRET") OKX_API_PASSPHRASE = os.getenv("OKX_API_PASSPHRASE")

TAXA_TAKER = Decimal("0.001") MIN_PROFIT_DEFAULT = Decimal("0.05") MOEDA_BASE_OPERACIONAL = 'USDT' MINIMO_ABSOLUTO_USDT = Decimal("3.1") MIN_ROUTE_DEPTH = 3 MAX_ROUTE_DEPTH_DEFAULT = 3 MARGEM_PRECO_TAKER = Decimal("1.0001") BLACKLIST_DURATION_SECONDS = 3600 # 1 hora para erros genéricos BLACKLIST_OKX_RESTRICTION = 604800 # 7 dias para erros de restrição

Lista de moedas fiduciárias para serem ignoradas

FIAT_CURRENCIES = {'BRL', 'USD', 'EUR', 'JPY', 'GBP', 'AUD', 'CAD', 'CHF', 'CNY'}

==============================================================================

2. CLASSE DO MOTOR DE ARBITRAGEM (GenesisEngine)

==============================================================================

class GenesisEngine: def init(self, application: Application): self.app = application self.bot_data = application.bot_data self.exchange = None

# Carrega configurações do arquivo
    self.config = self._load_config()
    
    # Configurações do Bot
    self.bot_data.setdefault('is_running', self.config.get('is_running', True))
    self.bot_data.setdefault('min_profit', Decimal(str(self.config.get('min_profit', MIN_PROFIT_DEFAULT))))
    self.bot_data.setdefault('dry_run', self.config.get('dry_run', True))
    self.bot_data.setdefault('volume_percent', Decimal(str(self.config.get('volume_percent', 100.0))))
    self.bot_data.setdefault('max_depth', self.config.get('max_depth', MAX_ROUTE_DEPTH_DEFAULT))
    # armazenamos stop_loss como valor POSITIVO em USDT
    self.bot_data.setdefault('stop_loss_usdt', Decimal(str(self.config.get('stop_loss_usdt'))) if self.config.get('stop_loss_usdt') is not None else None)
    
    # Dados Operacionais
    self.markets = {}
    self.graph = {}
    self.rotas_viaveis = []
    self.ecg_data = []
    self.current_cycle_results = []
    self.trade_lock = asyncio.Lock()
    
    # BLACKLIST AGORA PERSISTENTE
    self.blacklist = self.config.get('blacklist', {}) # { 'pair_id': timestamp_to_ignore_until }
    
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
        # Convert Decimals to floats para serializar
        config_data = {
            "is_running": bool(self.bot_data.get('is_running', True)),
            "min_profit": float(self.bot_data.get('min_profit', float(MIN_PROFIT_DEFAULT))),
            "dry_run": bool(self.bot_data.get('dry_run', True)),
            "volume_percent": float(self.bot_data.get('volume_percent', 100.0)),
            "max_depth": int(self.bot_data.get('max_depth', MAX_ROUTE_DEPTH_DEFAULT)),
            "stop_loss_usdt": float(self.bot_data.get('stop_loss_usdt')) if self.bot_data.get('stop_loss_usdt') is not None else None,
            "blacklist": self.blacklist,
        }
        with open('config.json', 'w') as f:
            json.dump(config_data, f, indent=2)
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
    # prioriza coin_to/coin_from porque em rotas de conversão a ordem pode inverter
    pair_id = f"{coin_to}/{coin_from}"
    if pair_id in self.markets:
        return pair_id, 'buy', 'ask'
    pair_id = f"{coin_from}/{coin_to}"
    if pair_id in self.markets:
        return pair_id, 'sell', 'bid'
        
    return None, None, None
    
def _is_blacklisted(self, pair_id):
    """Verifica se um par está na lista de bloqueio e, se sim, se já expirou."""
    if not pair_id: return False
    if pair_id in self.blacklist:
        if time.time() > self.blacklist[pair_id]:
            try:
                del self.blacklist[pair_id]
            except KeyError:
                pass
            self.save_config()
            return False
        return True
    return False

def _add_to_blacklist(self, pair_id: str, duration_seconds: int):
    """Adiciona um par à blacklist somente se pair_id for válido (não vazio)."""
    if not pair_id:
        logger.debug("Tentativa de adicionar par inválido/None à blacklist ignorada.")
        return
    self.blacklist[pair_id] = time.time() + duration_seconds
    self.save_config()

def _extract_min_notional(self, market: dict) -> Decimal:
    """Tenta extrair o mínimo notional de diferentes formatos que exchanges usam."""
    try:
        limits = market.get('limits', {}) if market else {}
        # possibilidades comuns: limits['notional']['min'], limits['cost']['min'], limits['value']['min']
        candidates = []
        if isinstance(limits.get('notional'), dict):
            candidates.append(limits.get('notional').get('min'))
        if isinstance(limits.get('cost'), dict):
            candidates.append(limits.get('cost').get('min'))
        if isinstance(limits.get('value'), dict):
            candidates.append(limits.get('value').get('min'))
        # alguns mercados usam limits['amount']['min'] * price para noção; não é seguro calcular aqui.
        for c in candidates:
            if c is not None:
                try:
                    val = Decimal(str(c))
                    if val >= 0:
                        return val
                except Exception:
                    continue
    except Exception:
        pass
    return Decimal('0')
    
async def verificar_oportunidades(self):
    logger.info("Motor 'Análise de Viabilidade' (v17.40) iniciado.")
    while True:
        await asyncio.sleep(5)
        if not self.bot_data.get('is_running', True) or self.trade_lock.locked():
            self.bot_data['progress_status'] = f"Pausado. Próxima verificação em 10s."
            await asyncio.sleep(10)
            continue

        # Aplicação simples do stop-loss diário (se definido)
        stop_loss = self.bot_data.get('stop_loss_usdt')
        if stop_loss is not None:
            try:
                if Decimal(str(self.bot_data.get('daily_profit_usdt', Decimal('0')))) <= -Decimal(str(stop_loss)):
                    self.bot_data['is_running'] = False
                    self.save_config()
                    await send_telegram_message(f"⛔ Stop-loss atingido: prejuízo diário >= {stop_loss} USDT. Motor pausado.")
                    continue
            except Exception:
                # se algo falhar, não interrompe a checagem
                pass

        self.stats['ciclos_verificacao_total'] += 1
        logger.info(f"Iniciando ciclo de verificação #{self.stats['ciclos_verificacao_total']}...")

        try:
            balance = await self.exchange.fetch_balance()
            saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
            
            volume_a_usar = saldo_disponivel * (self.bot_data['volume_percent'] / 100)
            
            if volume_a_usar < MINIMO_ABSOLUTO_USDT:
                self.bot_data['progress_status'] = f"Volume de trade ({volume_a_usar:.2f} USDT) abaixo do mínimo. Aguardando."
                await asyncio.sleep(30)
                continue

            self.current_cycle_results = []
            total_rotas = len(self.rotas_viaveis)

            for i, cycle_tuple in enumerate(self.rotas_viaveis):
                self.bot_data['progress_status'] = f"Analisando... Rota {i+1}/{total_rotas}."
                
                # verifica blacklist para quaisquer pares da rota (direto ou invertido)
                if any(self._is_blacklisted(f"{cycle_tuple[j]}/{cycle_tuple[j+1]}") or self._is_blacklisted(f"{cycle_tuple[j+1]}/{cycle_tuple[j]}") for j in range(len(cycle_tuple) - 1)):
                    logger.debug(f"Rota {' -> '.join(cycle_tuple)} ignorada (contém par na blacklist temporária).")
                    self.stats['rotas_filtradas'] += 1
                    continue
                    
                try:
                    # O bot AGORA VAI DIRETO PARA A SIMULAÇÃO DETALHADA
                    resultado = await self._simular_trade(list(cycle_tuple), volume_a_usar)
                    if resultado and resultado['profit'] > self.bot_data['min_profit']:
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

async def _simular_trade(self, cycle_path, volume_inicial):
    """
    Simula o trade em uma rota, consumindo o orderbook para maior precisão.
    """
    current_amount = Decimal(str(volume_inicial))
    
    for i in range(len(cycle_path) - 1):
        coin_from = cycle_path[i]
        coin_to = cycle_path[i+1]
        pair_id, side, _ = self._get_pair_details(coin_from, coin_to)
        
        if not pair_id or self._is_blacklisted(pair_id):
            return None
        
        try:
            orderbook = await self.exchange.fetch_order_book(pair_id)
        except Exception as e:
            logger.warning(f"Erro ao buscar orderbook para {pair_id}: {e}")
            return None

        orders = orderbook['asks'] if side == 'buy' else orderbook['bids']
        
        if not orders:
            return None
        
        amount_to_convert = current_amount
        converted_amount = Decimal('0')

        for order in orders:
            if len(order) < 2: continue
            price, size = Decimal(str(order[0])), Decimal(str(order[1]))

            if side == 'buy':
                cost = price * size
                if amount_to_convert >= cost:
                    converted_amount += size
                    amount_to_convert -= cost
                else:
                    converted_amount += amount_to_convert / price
                    amount_to_convert = Decimal('0')
                    break
            else: # side == 'sell'
                if amount_to_convert >= size:
                    converted_amount += size * price
                    amount_to_convert -= size
                else:
                    converted_amount += amount_to_convert * price
                    amount_to_convert = Decimal('0')
                    break

        if amount_to_convert > 0:
            # Não havia liquidez suficiente para a simulação completa
            return None
        
        current_amount = converted_amount * (Decimal('1') - TAXA_TAKER)
        
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
        lucro_simulado = self.ecg_data[0]['profit'] if self.ecg_data else Decimal('0')
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
                raise ValueError(f"Par inválido: {coin_from}/{coin_to}")
            
            if self._is_blacklisted(pair_id):
                raise ValueError(f"Par {pair_id} está na lista de bloqueio temporária. Ignorando a execução.")

            # RE-VERIFICAÇÃO DE SALDO ANTES DO TRADE
            balance = await self.exchange.fetch_balance()
            current_balance = Decimal(str(balance.get('free', {}).get(coin_from, '0')))
            
            if current_balance < current_amount_asset * Decimal('0.95'): # Margem de 5% para evitar falhas de precisão
                logger.error(f"❌ Saldo de {coin_from} insuficiente para o próximo passo. Saldo: {current_balance}, Necessário: {current_amount_asset}. Abortando rota e executando saída de emergência.")
                await self._executar_saida_de_emergencia(coin_from)
                return

            orderbook = await self.exchange.fetch_order_book(pair_id)
            if not orderbook.get('asks') or not orderbook.get('bids'):
                raise ValueError(f"Orderbook vazio para o par {pair_id}.")
            
            market = self.exchange.market(pair_id)

            # === NOVA LÓGICA DE VALIDAÇÃO DE PRECISÃO E NOTIONAL ===
            # Obtém limites de precisão e volume da corretora
            min_amount_market = Decimal('0')
            try:
                min_amount_market = Decimal(str(market.get('limits', {}).get('amount', {}).get('min', 0)))
            except Exception:
                min_amount_market = Decimal('0')

            # Extrai notional com função robusta
            min_notional_market = self._extract_min_notional(market)

            if side == 'sell':
                limit_price = Decimal(str(orderbook['bids'][0][0])) / MARGEM_PRECO_TAKER
                raw_amount_to_trade = current_amount_asset
            else: # side == 'buy'
                limit_price = Decimal(str(orderbook['asks'][0][0])) * MARGEM_PRECO_TAKER
                raw_amount_to_trade = current_amount_asset / limit_price
            
            # Arredonda o preço e o volume para a precisão exata da exchange
            limit_price = Decimal(str(self.exchange.price_to_precision(pair_id, limit_price)))
            amount_to_trade = Decimal(str(self.exchange.amount_to_precision(pair_id, raw_amount_to_trade)))
            
            notional_value = amount_to_trade * limit_price

            # Validação estrita dos valores arredondados
            if min_amount_market and amount_to_trade < min_amount_market:
                raise ValueError(f"Volume calculado `{amount_to_trade}` é muito baixo para o par `{pair_id}` (mínimo: {min_amount_market}).")
            
            if min_notional_market and notional_value < min_notional_market:
                raise ValueError(f"Valor nocional ({notional_value:.2f} {market['quote']}) é inferior ao mínimo ({min_notional_market}) da OKX para o par `{pair_id}`.")
            # === FIM DA NOVA LÓGICA ===

            logger.info(f"Tentando ordem LIMIT: {side.upper()} {amount_to_trade} de {pair_id} @ {limit_price}")

            limit_order = await self.exchange.create_order(
                symbol=pair_id,
                type='limit',
                side=side,
                amount=amount_to_trade,
                price=limit_price,
            )
            
            await asyncio.sleep(3)
            # sempre busca o status mais recente da ordem
            try:
                order_status = await self.exchange.fetch_order(limit_order['id'], pair_id)
            except Exception:
                # em casos raros fetch falha logo após create; tenta buscar novamente
                await asyncio.sleep(1)
                order_status = await self.exchange.fetch_order(limit_order['id'], pair_id)
            
            if order_status.get('status') == 'closed':
                logger.info(f"✅ Ordem LIMIT preenchida com sucesso!")
            else:
                logger.warning(f"❌ Ordem LIMIT não preenchida. Tentando cancelar e usar ordem a MERCADO.")
                
                try:
                    await self.exchange.cancel_order(limit_order['id'], pair_id)
                    # re-fetch para garantir remaining atualizado
                    await asyncio.sleep(0.5)
                    order_status = await self.exchange.fetch_order(limit_order['id'], pair_id)
                except Exception as e_cancel:
                    # algumas vezes o cancel retorna erro porque a ordem foi preenchida em "race condition"
                    logger.info(f"Cancel/Fetch posterior: {e_cancel}")
                    try:
                        order_status = await self.exchange.fetch_order(limit_order['id'], pair_id)
                    except Exception:
                        order_status = {'remaining': '0', 'filled': '0', 'id': limit_order.get('id')}

                remaining_amount_to_trade = Decimal('0')
                try:
                    remaining_amount_to_trade = Decimal(str(order_status.get('remaining', '0')))
                except Exception:
                    # fallback: calculamos a diferença entre amount_to_trade e filled, se possível
                    try:
                        filled = Decimal(str(order_status.get('filled', '0')))
                        remaining_amount_to_trade = amount_to_trade - filled
                    except Exception:
                        remaining_amount_to_trade = amount_to_trade

                if remaining_amount_to_trade > 0:
                    market_order = await self.exchange.create_order(
                        symbol=pair_id,
                        type='market',
                        side=side,
                        amount=remaining_amount_to_trade
                    )
                    # busca status da ordem market
                    try:
                        order_status = await self.exchange.fetch_order(market_order['id'], pair_id)
                    except Exception:
                        await asyncio.sleep(0.5)
                        order_status = await self.exchange.fetch_order(market_order['id'], pair_id)

                    if order_status.get('status') != 'closed':
                        raise Exception(f"Ordem de MERCADO não preenchida: {order_status.get('id')}")
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
        
        except ccxt.ExchangeError as e:
            msg = str(e)
            # procura códigos ou mensagens de restrição comuns
            if '51155' in msg or 'You can\'t trade' in msg or 'compliance' in msg.lower():
                logger.error(f"❌ Erro de restrição da OKX para {pair_id}: {e}")
                await send_telegram_message(f"❌ **Falha na Execução do Trade:** A corretora rejeitou a negociação de `{pair_id}` devido a restrições de conformidade. O par foi adicionado à lista de bloqueio de longo prazo por 7 dias para evitar novas tentativas.")
                self._add_to_blacklist(pair_id, BLACKLIST_OKX_RESTRICTION)
                return # Interrompe a execução da rota atual
            else:
                # re-raise para ser tratado pelo except geral
                raise e
        except Exception as e:
            logger.error(f"❌ Falha ao executar trade de {coin_from} para {coin_to}: {e}", exc_info=True)
            await send_telegram_message(f"❌ **Falha na Execução do Trade:** Algo deu errado na rota `{coin_from} -> {coin_to}`. Erro: `{e}`")
            
            if i > 0:
                asset_to_dump = cycle_path[i]
                await self._executar_saida_de_emergencia(asset_to_dump)
            
            # adiciona à blacklist apenas se pair_id for válido
            fallback_pair = pair_id if pair_id else f"{coin_from}/{coin_to}"
            self._add_to_blacklist(fallback_pair, BLACKLIST_DURATION_SECONDS)
            return

    final_amount = current_amount_asset
    lucro_real_percent = ((final_amount - volume_a_usar) / volume_a_usar) * 100
    lucro_real_usdt = final_amount - volume_a_usar
    
    self.stats['trades_executados'] += 1
    self.stats['lucro_total_sessao'] += lucro_real_usdt
    self.bot_data['daily_profit_usdt'] += lucro_real_usdt
    self.save_config()

    await send_telegram_message(f"✅ **Arbitragem Executada com Sucesso!**\nRota: `{' -> '.join(cycle_path)}`\nVolume: `{volume_a_usar:.4f} USDT`\nLucro: `{lucro_real_usdt:.4f} USDT` (`{lucro_real_percent:.4f}%`)")

async def send_telegram_message(text): if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return try: bot = Bot(token=TELEGRAM_TOKEN) await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown") except Exception as e: logger.error(f"Erro ao enviar mensagem no Telegram: {e}")

-----------------------------------------------------------------------------

Comandos do Telegram (pequenas correções no set_stoploss para salvar configuração)

-----------------------------------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: help_text = f""" 👋 Olá! Sou o Gênesis v17.40, seu bot de arbitragem. Estou monitorando o mercado 24/7 para encontrar oportunidades. Use /ajuda para ver a lista de comandos. """ await update.message.reply_text(help_text, parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: dry_run = context.bot_data.get('dry_run', True) status_text = "Em operação" if context.bot_data.get('is_running', True) else "Pausado" dry_run_text = "Simulação (Dry Run)" if dry_run else "Modo Real"

response = f"""

🤖 Status do Gênesis v17.40: Status: {status_text} Modo: {dry_run_text} Lucro Mínimo: {context.bot_data.get('min_profit'):.4f}% Volume de Trade: {context.bot_data.get('volume_percent'):.2f}% do saldo Profundidade de Rotas: {context.bot_data.get('max_depth')} Stop Loss: {context.bot_data.get('stop_loss_usdt') or 'Não definido'} USDT

Progresso: {context.bot_data.get('progress_status')} """ await update.message.reply_text(response, parse_mode="Markdown")

async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE): engine = context.bot_data.get('engine') if not engine or not engine.exchange: await update.message.reply_text("Engine não inicializada. Tente novamente mais tarde.") return

try:
    balance = await engine.exchange.fetch_balance()
    saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
    
    response = f"📊 **Saldo OKX:**\n`{saldo_disponivel:.4f} {MOEDA_BASE_OPERACIONAL}` disponível."
    await update.message.reply_text(response, parse_mode="Markdown")
except Exception as e:
    await update.message.reply_text(f"❌ Erro ao buscar saldo: {e}")

async def modo_real_command(update: Update, context: ContextTypes.DEFAULT_TYPE): context.bot_data['dry_run'] = False context.bot_data['engine'].save_config() await update.message.reply_text("✅ Modo Real Ativado!\nO bot agora executará ordens de verdade. Use com cautela.")

async def modo_simulacao_command(update: Update, context: ContextTypes.DEFAULT_TYPE): context.bot_data['dry_run'] = True context.bot_data['engine'].save_config() await update.message.reply_text("✅ Modo Simulação Ativado!\nO bot apenas simulará trades.")

async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE): try: min_profit = Decimal(context.args[0]) if min_profit < 0: raise ValueError context.bot_data['min_profit'] = min_profit context.bot_data['engine'].save_config() await update.message.reply_text(f"✅ Lucro mínimo definido para {min_profit:.4f}%.") except (ValueError, IndexError): await update.message.reply_text("❌ Uso incorreto. Use: /setlucro <porcentagem> (ex: /setlucro 0.1)")

async def setvolume_command(update: Update, context: ContextTypes.DEFAULT_TYPE): try: volume_percent = Decimal(context.args[0]) if not (0 < volume_percent <= 100): raise ValueError context.bot_data['volume_percent'] = volume_percent context.bot_data['engine'].save_config() await update.message.reply_text(f"✅ Volume de trade definido para {volume_percent:.2f}% do saldo.") except (ValueError, IndexError): await update.message.reply_text("❌ Uso incorreto. Use: /setvolume <porcentagem> (ex: /setvolume 50)")

async def pausar_command(update: Update, context: ContextTypes.DEFAULT_TYPE): context.bot_data['is_running'] = False context.bot_data['engine'].save_config() await update.message.reply_text("⏸️ Motor de arbitragem pausado.")

async def retomar_command(update: Update, context: ContextTypes.DEFAULT_TYPE): context.bot_data['is_running'] = True context.bot_data['engine'].save_config() await update.message.reply_text("▶️ Motor de arbitragem retomado.")

async def set_stoploss_command(update: Update, context: ContextTypes.DEFAULT_TYPE): try: stop_loss_value = context.args[0].lower() if stop_loss_value == 'off': context.bot_data['stop_loss_usdt'] = None context.bot_data['engine'].save_config() await update.message.reply_text("✅ Stop Loss desativado.") else: stop_loss = Decimal(stop_loss_value) if stop_loss <= 0: raise ValueError # Armazenamos POSITIVO context.bot_data['stop_loss_usdt'] = stop_loss context.bot_data['engine'].save_config() await update.message.reply_text(f"✅ Stop Loss definido para {stop_loss:.2f} USDT.") except (ValueError, IndexError): await update.message.reply_text("❌ Uso incorreto. Use: /set_stoploss <valor> (ex: /set_stoploss 100) ou /set_stoploss off.")

async def rotas_command(update: Update, context: ContextTypes.DEFAULT_TYPE): engine = context.bot_data.get('engine') if engine and engine.ecg_data: top_rotas = "\n".join([ f"{i+1}. {' -> '.join(r['cycle'])}\n   Lucro Simulado: {r['profit']:.4f}%" for i, r in enumerate(engine.ecg_data[:5]) ]) response = f"📈 Rotas mais Lucrativas (Simulação):\n\n{top_rotas}" await update.message.reply_text(response, parse_mode="Markdown") return await update.message.reply_text("Ainda não há dados de rotas. O bot pode estar em um ciclo inicial de análise.")

async def ajuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE): help_text = """ 📚 Lista de Comandos: /start - Mensagem de boas-vindas. /status - Mostra o status atual do bot. /saldo - Exibe o saldo disponível em USDT. /modo_real - Ativa o modo de negociação real. /modo_simulacao - Ativa o modo de simulação. /setlucro <%> - Define o lucro mínimo para executar (ex: 0.1). /setvolume <%> - Define a porcentagem do saldo a usar (ex: 50). /pausar - Pausa o motor de arbitragem. /retomar - Retoma o motor. /set_stoploss <valor> - Define stop loss em USDT. Use 'off' para desativar. /rotas - Mostra as 5 rotas mais lucrativas simuladas. /ajuda - Exibe esta lista de comandos. /stats - Estatísticas da sessão. /setdepth <n> - Define a profundidade máxima das rotas (padrão: 3). /progresso - Mostra o status atual do ciclo de análise. """ await update.message.reply_text(help_text, parse_mode="Markdown")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE): engine = context.bot_data.get('engine') if not engine: await update.message.reply_text("Engine não inicializada.") return stats = engine.stats uptime = time.time() - stats['start_time'] uptime_str = time.strftime("%Hh %Mm %Ss", time.gmtime(uptime))

response = f"""

📊 Estatísticas da Sessão: Tempo de Atividade: {uptime_str} Ciclos de Verificação: {stats['ciclos_verificacao_total']} Rotas Filtradas: {stats['rotas_filtradas']} Trades Executados: {stats['trades_executados']} Lucro Total (Sessão): {stats['lucro_total_sessao']:.4f} {MOEDA_BASE_OPERACIONAL} Erros de Simulação: {stats['erros_simulacao']} """ await update.message.reply_text(response, parse_mode="Markdown")

async def setdepth_command(update: Update, context: ContextTypes.DEFAULT_TYPE): engine = context.bot_data.get('engine') if not engine: await update.message.reply_text("Engine não inicializada.") return try: depth = int(context.args[0]) if not (MIN_ROUTE_DEPTH <= depth <= 5): raise ValueError context.bot_data['max_depth'] = depth await engine.construir_rotas(depth) context.bot_data['engine'].save_config() await update.message.reply_text(f"✅ Profundidade máxima das rotas definida para {depth}. Rotas recalculadas.") except (ValueError, IndexError): await update.message.reply_text(f"❌ Uso incorreto. Use: /setdepth <número> (min: {MIN_ROUTE_DEPTH}, max: 5)")

async def progresso_command(update: Update, context: ContextTypes.DEFAULT_TYPE): status_text = context.bot_data.get('progress_status', 'Status não disponível.') await update.message.reply_text(f"⚙️ Progresso Atual:\n{status_text}")

async def post_init_tasks(app: Application): logger.info("Iniciando motor Gênesis v17.40 'Revisão Completa'...") engine = GenesisEngine(app) app.bot_data['engine'] = engine await send_telegram_message("🤖 Gênesis v17.40 'Revisão Completa' iniciado.\nAs configurações agora são salvas e carregadas automaticamente.") if await engine.inicializar_exchange(): await engine.construir_rotas(app.bot_data['max_depth']) asyncio.create_task(engine.verificar_oportunidades()) logger.info("Motor e tarefas de fundo iniciadas.") else: await send_telegram_message("❌ ERRO CRÍTICO: Não foi possível conectar à OKX.") if engine.exchange: await engine.exchange.close()

def main(): if not TELEGRAM_TOKEN: logger.critical("Token do Telegram não encontrado."); return application = Application.builder().token(TELEGRAM_TOKEN).build()

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

if name == "main": main()

