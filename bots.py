-- coding: utf-8 --

""" Gênesis v17.40 — Revisão completa e correções

Corrigidos cabeçalhos e comentários inválidos que quebravam o Python.

Tratamento robusto para limites da exchange (amount/cost/notional) e ausências.

Execução de ordens com rechecagem de status, cálculo de preço médio seguro e fallback.

Proteções contra blacklisting de chave None e persistência consistente.

Stop-loss diário funcional (valor POSITIVO), com reset diário UTC.

Logs e mensagens Telegram mais claros.


ATENÇÃO: Teste em /modo_simulacao antes do modo real. """

import os import asyncio import logging from decimal import Decimal, getcontext import time from datetime import datetime import json from typing import List, Dict, Tuple, Optional

=== IMPORTAÇÃO CCXT E TELEGRAM ===

try: import ccxt.async_support as ccxt from telegram import Update, Bot from telegram.ext import Application, CommandHandler, ContextTypes except ImportError: print("Erro: Bibliotecas essenciais não instaladas.") ccxt = None  # type: ignore Bot = None   # type: ignore

==============================================================================

1. CONFIGURAÇÃO GLOBAL E INICIALIZAÇÃO

==============================================================================

logging.basicConfig( format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO ) logger = logging.getLogger(name) getcontext().prec = 30

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") OKX_API_KEY = os.getenv("OKX_API_KEY") OKX_API_SECRET = os.getenv("OKX_API_SECRET") OKX_API_PASSPHRASE = os.getenv("OKX_API_PASSPHRASE")

TAXA_TAKER = Decimal("0.001")  # 0.1% MIN_PROFIT_DEFAULT = Decimal("0.05")  # 0.05% MOEDA_BASE_OPERACIONAL = 'USDT' MINIMO_ABSOLUTO_USDT = Decimal("3.1") MIN_ROUTE_DEPTH = 3 MAX_ROUTE_DEPTH_DEFAULT = 3 MARGEM_PRECO_TAKER = Decimal("1.0001") BLACKLIST_DURATION_SECONDS = 3600           # 1h para erros genéricos BLACKLIST_OKX_RESTRICTION = 7 * 24 * 3600   # 7 dias para restrições

Lista de moedas fiduciárias para ignorar no grafo (FIAT ≠ stablecoins)

FIAT_CURRENCIES = {'BRL', 'USD', 'EUR', 'JPY', 'GBP', 'AUD', 'CAD', 'CHF', 'CNY'}

==============================================================================

2. FUNÇÕES AUXILIARES

==============================================================================

def _dec(value, default: str = '0') -> Decimal: """Converte com segurança para Decimal.""" try: if value is None: return Decimal(default) return Decimal(str(value)) except Exception: return Decimal(default)

def _safe_get_min_limits(market: dict) -> Tuple[Decimal, Decimal]: """ Retorna (min_amount, min_cost) com fallback para diferentes formatos de CCXT/OKX. min_cost também é conhecido como "notional" ou "cost" mínimo em algumas exchanges. """ limits = market.get('limits', {}) or {}

# min amount
min_amount = _dec(
    ((limits.get('amount') or {}).get('min'))
    or (market.get('info') or {}).get('minSz')
    or 0
)

# min cost / notional (variável entre 'cost', 'notional' e 'qMin')
possible_cost_paths = [
    (limits.get('cost') or {}).get('min'),
    (limits.get('notional') or {}).get('min'),
    (market.get('info') or {}).get('minNotional'),
    (market.get('info') or {}).get('minSzQuote'),
    0,
]
min_cost = _dec(next((v for v in possible_cost_paths if v is not None), 0))

# Evitar negativos ou NaN
if min_amount < 0:
    min_amount = Decimal('0')
if min_cost < 0:
    min_cost = Decimal('0')

return (min_amount, min_cost)

def _extract_order_fills(order: dict) -> Tuple[Decimal, Decimal, Decimal]: """ Extrai (filled_base, avg_price, remaining_base) de uma ordem CCXT com fallbacks. """ filled = _dec(order.get('filled')) remaining = _dec(order.get('remaining'))

avg = order.get('average')
price = order.get('price')
cost = order.get('cost')

avg_price = _dec(avg) if avg is not None else (
    _dec(price) if price is not None else (
        (_dec(cost) / filled) if (filled > 0 and cost is not None) else Decimal('0')
    )
)

return (filled, avg_price, remaining)

==============================================================================

3. CLASSE DO MOTOR DE ARBITRAGEM (GenesisEngine)

==============================================================================

class GenesisEngine: def init(self, application: Application): self.app = application self.bot_data = application.bot_data self.exchange: Optional[ccxt.Exchange] = None  # type: ignore

# Carrega configurações do arquivo
    self.config = self._load_config()

    # Configurações do Bot
    self.bot_data.setdefault('is_running', self.config.get('is_running', True))
    self.bot_data.setdefault('min_profit', _dec(self.config.get('min_profit', MIN_PROFIT_DEFAULT)))
    self.bot_data.setdefault('dry_run', self.config.get('dry_run', True))
    self.bot_data.setdefault('volume_percent', _dec(self.config.get('volume_percent', 100)))
    self.bot_data.setdefault('max_depth', int(self.config.get('max_depth', MAX_ROUTE_DEPTH_DEFAULT)))
    # Stop-loss POSITIVO (ex.: 100 => pausa ao atingir -100 no dia)
    sl = self.config.get('stop_loss_usdt', None)
    self.bot_data.setdefault('stop_loss_usdt', (_dec(sl) if sl is not None else None))

    # Dados Operacionais
    self.markets: Dict[str, dict] = {}
    self.graph: Dict[str, List[str]] = {}
    self.rotas_viaveis: List[Tuple[str, ...]] = []
    self.ecg_data: List[Dict] = []
    self.current_cycle_results: List[Dict] = []
    self.trade_lock = asyncio.Lock()

    # BLACKLIST PERSISTENTE {pair_id: epoch_expira}
    raw_bl = self.config.get('blacklist', {}) or {}
    # Sanear chaves não-string
    self.blacklist: Dict[str, float] = {str(k): float(v) for k, v in raw_bl.items() if isinstance(k, str)}

    # Status e Estatísticas
    self.bot_data.setdefault('daily_profit_usdt', Decimal('0'))
    self.bot_data.setdefault('last_reset_day', datetime.utcnow().day)
    self.stats = {
        'start_time': time.time(),
        'ciclos_verificacao_total': 0,
        'trades_executados': 0,
        'lucro_total_sessao': Decimal('0'),
        'erros_simulacao': 0,
        'rotas_filtradas': 0,
    }
    self.bot_data['progress_status'] = "Iniciando..."

# -------------------------
# Persistência de config
# -------------------------
def _load_config(self):
    try:
        with open('config.json', 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning("Arquivo 'config.json' não encontrado ou inválido. Usando padrões.")
        return {}

def save_config(self):
    try:
        config_data = {
            "is_running": bool(self.bot_data.get('is_running', True)),
            "min_profit": float(self.bot_data.get('min_profit', MIN_PROFIT_DEFAULT)),
            "dry_run": bool(self.bot_data.get('dry_run', True)),
            "volume_percent": float(self.bot_data.get('volume_percent', 100)),
            "max_depth": int(self.bot_data.get('max_depth', MAX_ROUTE_DEPTH_DEFAULT)),
            # Armazena como positivo
            "stop_loss_usdt": (float(self.bot_data['stop_loss_usdt']) if self.bot_data.get('stop_loss_usdt') is not None else None),
            "blacklist": self.blacklist,
        }
        with open('config.json', 'w') as f:
            json.dump(config_data, f, indent=2)
        logger.info("Configurações salvas.")
    except Exception as e:
        logger.error(f"Erro ao salvar configurações: {e}")

# -------------------------
# Exchange
# -------------------------
async def inicializar_exchange(self) -> bool:
    if not ccxt:
        return False
    if not all([OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSPHRASE]):
        await send_telegram_message("❌ Falha crítica: Verifique as chaves da API da OKX na Heroku.")
        return False
    try:
        self.exchange = ccxt.okx({
            'apiKey': OKX_API_KEY,
            'secret': OKX_API_SECRET,
            'password': OKX_API_PASSPHRASE,
            'options': {'defaultType': 'spot'},
        })
        self.markets = await self.exchange.load_markets()
        logger.info(f"Conectado à OKX. {len(self.markets)} mercados carregados.")
        return True
    except Exception as e:
        logger.critical(f"❌ Falha ao conectar com a OKX: {e}", exc_info=True)
        await send_telegram_message(f"❌ Erro de Conexão com a OKX: `{type(e).__name__}`.")
        if self.exchange:
            await self.exchange.close()
        return False

# -------------------------
# Rotas
# -------------------------
async def construir_rotas(self, max_depth: int):
    self.bot_data['progress_status'] = "Construindo mapa de rotas..."
    logger.info(f"Construindo mapa (Profundidade: {max_depth})...")
    self.graph = {}

    active_markets = {
        s: m for s, m in self.markets.items()
        if m.get('active') and m.get('base') and m.get('quote')
        and m['base'] not in FIAT_CURRENCIES and m['quote'] not in FIAT_CURRENCIES
    }

    for symbol, market in active_markets.items():
        base, quote = market['base'], market['quote']
        self.graph.setdefault(base, []).append(quote)
        self.graph.setdefault(quote, []).append(base)

    logger.info(f"Mapa construído com {len(self.graph)} nós. Buscando rotas...")
    self.rotas_viaveis = []

    def dfs(u: str, path: List[str], depth: int):
        if depth > max_depth:
            return
        for v in self.graph.get(u, []):
            if v == MOEDA_BASE_OPERACIONAL and len(path) >= MIN_ROUTE_DEPTH:
                rota = path + [v]
                # Sem moedas repetidas no meio da rota (exclui o retorno final)
                if len(set(rota[:-1])) == len(rota[:-1]):
                    self.rotas_viaveis.append(tuple(rota))
            elif v not in path:
                dfs(v, path + [v], depth + 1)

    dfs(MOEDA_BASE_OPERACIONAL, [MOEDA_BASE_OPERACIONAL], 1)
    self.rotas_viaveis = list(set(self.rotas_viaveis))
    self.bot_data['total_rotas'] = len(self.rotas_viaveis)
    await send_telegram_message(f"🗺️ Mapa de rotas reconstruído. {self.bot_data['total_rotas']} rotas cripto-cripto serão monitoradas.")
    self.bot_data['progress_status'] = "Pronto para iniciar ciclos de análise."

def _get_pair_details(self, coin_from: str, coin_to: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Retorna (pair_id, side, book_side) para a troca coin_from -> coin_to."""
    # Para comprar coin_to usando coin_from: par `coin_to/coin_from`, side=buy
    pair_id = f"{coin_to}/{coin_from}"
    if pair_id in self.markets:
        return pair_id, 'buy', 'ask'
    # Para vender coin_from por coin_to: par `coin_from/coin_to`, side=sell
    pair_id = f"{coin_from}/{coin_to}"
    if pair_id in self.markets:
        return pair_id, 'sell', 'bid'
    return None, None, None

def _is_blacklisted(self, pair_id: Optional[str]) -> bool:
    if not pair_id:
        return False
    ts = self.blacklist.get(pair_id)
    if ts is None:
        return False
    if time.time() > ts:
        # Expirou
        try:
            del self.blacklist[pair_id]
            self.save_config()
        except Exception:
            pass
        return False
    return True

# -------------------------
# Loop principal
# -------------------------
async def verificar_oportunidades(self):
    logger.info("Motor 'Análise de Viabilidade' (v17.40) iniciado.")
    while True:
        await asyncio.sleep(5)

        # Reset diário (UTC)
        now_day = datetime.utcnow().day
        if now_day != int(self.bot_data.get('last_reset_day', now_day)):
            self.bot_data['last_reset_day'] = now_day
            self.bot_data['daily_profit_usdt'] = Decimal('0')
            self.save_config()
            logger.info("Reset diário de lucro realizado.")

        # Stop-loss diário
        sl = self.bot_data.get('stop_loss_usdt')
        if sl is not None and self.bot_data.get('daily_profit_usdt', Decimal('0')) <= (Decimal('0') - _dec(sl)):
            self.bot_data['is_running'] = False
            self.save_config()
            await send_telegram_message(f"⛔ Stop-loss diário atingido. Lucro diário: `{self.bot_data['daily_profit_usdt']:.4f} USDT`. Bot pausado.")
            await asyncio.sleep(30)
            continue

        if not self.bot_data.get('is_running', True) or self.trade_lock.locked():
            self.bot_data['progress_status'] = "Pausado. Próxima verificação em 10s."
            await asyncio.sleep(10)
            continue

        self.stats['ciclos_verificacao_total'] += 1
        logger.info(f"Iniciando ciclo de verificação #{self.stats['ciclos_verificacao_total']}...")

        try:
            assert self.exchange is not None
            balance = await self.exchange.fetch_balance()
            saldo_disponivel = _dec((balance.get('free', {}) or {}).get(MOEDA_BASE_OPERACIONAL, '0'))
            volume_percent = _dec(self.bot_data.get('volume_percent', 100))
            volume_a_usar = saldo_disponivel * (volume_percent / Decimal('100'))

            if volume_a_usar < MINIMO_ABSOLUTO_USDT:
                self.bot_data['progress_status'] = f"Volume de trade ({volume_a_usar:.2f} USDT) abaixo do mínimo. Aguardando."
                await asyncio.sleep(30)
                continue

            self.current_cycle_results = []
            total_rotas = len(self.rotas_viaveis)

            for i, cycle_tuple in enumerate(self.rotas_viaveis):
                self.bot_data['progress_status'] = f"Analisando... Rota {i+1}/{total_rotas}."

                # Pula rota que contenha par em blacklist
                skip = False
                for j in range(len(cycle_tuple) - 1):
                    a, b = cycle_tuple[j], cycle_tuple[j+1]
                    if self._is_blacklisted(f"{a}/{b}") or self._is_blacklisted(f"{b}/{a}"):
                        skip = True
                        break
                if skip:
                    self.stats['rotas_filtradas'] += 1
                    continue

                try:
                    resultado = await self._simular_trade(list(cycle_tuple), volume_a_usar)
                    if resultado and resultado['profit'] > self.bot_data['min_profit']:
                        self.current_cycle_results.append(resultado)
                except Exception as e:
                    self.stats['erros_simulacao'] += 1
                    logger.warning(f"Erro ao simular rota {cycle_tuple}: {e}")

                await asyncio.sleep(0.1)

            self.ecg_data = sorted(self.current_cycle_results, key=lambda x: x['profit'], reverse=True)
            self.current_cycle_results = []
            logger.info(
                f"Ciclo concluído. {len(self.ecg_data)} rotas OK. Erros: {self.stats['erros_simulacao']}. Filtradas: {self.stats['rotas_filtradas']}."
            )
            self.bot_data['progress_status'] = "Ciclo concluído. Aguardando próximo ciclo..."

            if self.ecg_data and self.ecg_data[0]['profit'] > self.bot_data['min_profit']:
                async with self.trade_lock:
                    await self._executar_trade(self.ecg_data[0]['cycle'], volume_a_usar)

        except Exception as e:
            logger.error(f"Erro CRÍTICO no loop de verificação: {e}", exc_info=True)
            await send_telegram_message(f"⚠️ **Erro Grave no Bot:** `{type(e).__name__}`. Verifique os logs.")
            self.bot_data['progress_status'] = "Erro crítico. Verifique os logs."

# -------------------------
# Simulação
# -------------------------
async def _simular_trade(self, cycle_path: List[str], volume_inicial: Decimal):
    assert self.exchange is not None
    current_amount = _dec(volume_inicial)

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
            if len(order) < 2:
                continue
            price, size = _dec(order[0]), _dec(order[1])

            if side == 'buy':
                cost = price * size  # quote necessário para consumir esta oferta
                if amount_to_convert >= cost:
                    converted_amount += size
                    amount_to_convert -= cost
                else:
                    converted_amount += (amount_to_convert / price)
                    amount_to_convert = Decimal('0')
                    break
            else:  # sell
                if amount_to_convert >= size:
                    converted_amount += size * price
                    amount_to_convert -= size
                else:
                    converted_amount += amount_to_convert * price
                    amount_to_convert = Decimal('0')
                    break

        if amount_to_convert > 0:  # liquidez insuficiente
            return None

        current_amount = converted_amount * (Decimal('1') - TAXA_TAKER)

    lucro_percentual = ((current_amount - volume_inicial) / volume_inicial) * 100
    if lucro_percentual > 0:
        return {'cycle': cycle_path, 'profit': lucro_percentual}
    return None

# -------------------------
# Saída de emergência
# -------------------------
async def _executar_saida_de_emergencia(self, asset: str):
    assert self.exchange is not None
    try:
        balance = await self.exchange.fetch_balance()
        asset_balance = _dec((balance.get('free', {}) or {}).get(asset, '0'))
        if asset_balance <= 0:
            logger.info(f"Sem saldo de {asset} para saída de emergência.")
            return

        pair_id = f"{asset}/{MOEDA_BASE_OPERACIONAL}"
        if pair_id not in self.markets:
            logger.error(f"Par de emergência {pair_id} não encontrado.")
            await send_telegram_message(
                f"❌ **Falha Crítica:** Não consegui vender `{asset}` de volta para `{MOEDA_BASE_OPERACIONAL}`. Saldo pode estar preso."
            )
            return

        amount_to_sell = _dec(str(self.exchange.amount_to_precision(pair_id, asset_balance)))
        logger.warning(f"🚨 Saída de EMERGÊNCIA: Vendendo {amount_to_sell} de {asset} para {MOEDA_BASE_OPERACIONAL}.")

        market_order = await self.exchange.create_order(
            symbol=pair_id,
            type='market',
            side='sell',
            amount=amount_to_sell
        )
        await send_telegram_message(
            f"⚠️ **Saída de Emergência:** Ordem de venda de `{amount_to_sell:.4f} {asset}` enviada ao mercado."
        )
    except Exception as e:
        logger.error(f"❌ Erro na saída de emergência para {asset}: {e}", exc_info=True)
        await send_telegram_message(f"❌ **Saída de Emergência Falhou:** Não foi possível vender `{asset}`. Erro: `{e}`")

# -------------------------
# Execução real
# -------------------------
async def _executar_trade(self, cycle_path: List[str], volume_a_usar: Decimal):
    assert self.exchange is not None
    logger.info(f"🚀 Oportunidade encontrada. Executando rota: {' -> '.join(cycle_path)}.")

    if self.bot_data.get('dry_run', True):
        lucro_simulado = self.ecg_data[0]['profit'] if self.ecg_data else Decimal('0')
        await send_telegram_message(
            f"✅ **Simulação:** Oportunidade encontrada e seria executada. Lucro simulado: `{lucro_simulado:.4f}%`."
        )
        self.stats['trades_executados'] += 1
        return

    current_amount_asset = _dec(volume_a_usar)

    for i in range(len(cycle_path) - 1):
        coin_from = cycle_path[i]
        coin_to = cycle_path[i + 1]
        pair_id, side, _ = self._get_pair_details(coin_from, coin_to)

        try:
            if not pair_id:
                raise ValueError(f"Par inválido: {coin_from}/{coin_to}")
            if self._is_blacklisted(pair_id):
                raise ValueError(f"Par {pair_id} está na blacklist temporária. Ignorando execução.")

            balance = await self.exchange.fetch_balance()
            current_balance = _dec((balance.get('free', {}) or {}).get(coin_from, '0'))

            # margem de 5% para evitar discretização/atraso de saldo
            if current_balance < (current_amount_asset * Decimal('0.95')):
                logger.error(
                    f"❌ Saldo de {coin_from} insuficiente. Saldo: {current_balance}, Necessário: {current_amount_asset}. Abortando e acionando emergência."
                )
                await self._executar_saida_de_emergencia(coin_from)
                return

            orderbook = await self.exchange.fetch_order_book(pair_id)
            if not orderbook.get('asks') or not orderbook.get('bids'):
                raise ValueError(f"Orderbook vazio para o par {pair_id}.")

            market = self.exchange.market(pair_id)
            min_amount_market, min_cost_market = _safe_get_min_limits(market)

            # Preços-alvo limit (leve margem)
            if side == 'sell':
                limit_price = _dec(orderbook['bids'][0][0]) / MARGEM_PRECO_TAKER
                raw_amount_to_trade = current_amount_asset
            else:  # buy
                limit_price = _dec(orderbook['asks'][0][0]) * MARGEM_PRECO_TAKER
                raw_amount_to_trade = (current_amount_asset / limit_price)

            # Ajustar para precisão da exchange
            limit_price = _dec(self.exchange.price_to_precision(pair_id, limit_price))
            amount_to_trade = _dec(self.exchange.amount_to_precision(pair_id, raw_amount_to_trade))
            notional_value = amount_to_trade * limit_price

            # Validações de limites
            if amount_to_trade <= 0:
                raise ValueError("Quantidade calculada <= 0 após precisão.")
            if min_amount_market > 0 and amount_to_trade < min_amount_market:
                raise ValueError(
                    f"Volume `{amount_to_trade}` abaixo do mínimo `{min_amount_market}` para `{pair_id}`."
                )
            if min_cost_market > 0 and notional_value < min_cost_market:
                raise ValueError(
                    f"Notional `{notional_value:.8f} {market['quote']}` < mínimo `{min_cost_market}` para `{pair_id}`."
                )

            logger.info(f"Tentando LIMIT: {side.upper()} {amount_to_trade} {pair_id} @ {limit_price}")
            limit_order = await self.exchange.create_order(
                symbol=pair_id,
                type='limit',
                side=side,
                amount=amount_to_trade,
                price=limit_price,
            )

            await asyncio.sleep(3)
            order_status = await self.exchange.fetch_order(limit_order['id'], pair_id)

            if order_status.get('status') == 'closed':
                logger.info("✅ Ordem LIMIT preenchida.")
            else:
                logger.warning("⏳ LIMIT não fechou. Tentando cancelar e enviar MARKET no restante.")
                try:
                    await self.exchange.cancel_order(limit_order['id'], pair_id)
                    # Atualiza status após cancelamento
                    order_status = await self.exchange.fetch_order(limit_order['id'], pair_id)
                except ccxt.OrderNotFound:
                    logger.info("✅ Ordem já fechada (race condition). Prosseguindo.")
                    order_status = await self.exchange.fetch_order(limit_order['id'], pair_id)
                except ccxt.ExchangeError as e:
                    if '51400' in str(e):  # código típico de ordem já fechada na OKX
                        logger.info("✅ Ordem preenchida durante cancelamento.")
                        order_status = await self.exchange.fetch_order(limit_order['id'], pair_id)
                    else:
                        raise

                filled_base, _, remaining_base = _extract_order_fills(order_status)

                # Se ainda resta, envia MARKET para completar
                if remaining_base > 0:
                    market_amount = _dec(self.exchange.amount_to_precision(pair_id, remaining_base))
                    if market_amount > 0:
                        market_order = await self.exchange.create_order(
                            symbol=pair_id,
                            type='market',
                            side=side,
                            amount=market_amount,
                        )
                        order_status = await self.exchange.fetch_order(market_order['id'], pair_id)
                        if order_status.get('status') != 'closed':
                            raise Exception(f"Ordem MARKET não fechou: {order_status.get('id')}")
                        logger.info("✅ MARKET restante preenchida.")

            # Métricas finais
            filled_base, avg_price, _ = _extract_order_fills(order_status)
            if filled_base <= 0:
                raise ValueError("Filled = 0 após execução.")

            if side == 'buy':
                # Compramos base usando quote. Atualiza quantidade de base (menos taxa)
                fee_amount_base = filled_base * TAXA_TAKER
                current_amount_asset = filled_base - fee_amount_base
            else:
                # Vendemos base por quote. Converte para quote (menos taxa)
                if avg_price <= 0:
                    raise ValueError("Preço médio inválido para calcular retorno da venda.")
                bruto_quote = filled_base * avg_price
                fee_quote = bruto_quote * TAXA_TAKER
                current_amount_asset = bruto_quote - fee_quote

        except ccxt.ExchangeError as e:  # Erros específicos da OKX/CCXT
            msg = str(e)
            if any(code in msg for code in ['51155', 'compliance', 'restricted']):
                logger.error(f"❌ Restrição de conformidade OKX para {pair_id}: {e}")
                await send_telegram_message(
                    f"❌ **Falha na Execução do Trade:** A corretora rejeitou `{pair_id}` por restrições de conformidade. Par bloqueado por 7 dias."
                )
                if pair_id:
                    self.blacklist[pair_id] = time.time() + BLACKLIST_OKX_RESTRICTION
                    self.save_config()
                return
            else:
                raise
        except Exception as e:
            logger.error(f"❌ Falha ao executar trade {coin_from} -> {coin_to}: {e}", exc_info=True)
            await send_telegram_message(
                f"❌ **Falha na Execução do Trade:** Algo deu errado na rota `{coin_from} -> {coin_to}`. Erro: `{e}`"
            )
            # Tenta destravar o passo atual (dump do ativo atual se não for o inicial)
            if i > 0:
                asset_to_dump = cycle_path[i]
                await self._executar_saida_de_emergencia(asset_to_dump)
            if pair_id:
                self.blacklist[pair_id] = time.time() + BLACKLIST_DURATION_SECONDS
                self.save_config()
            return

    # Finalização
    final_amount = current_amount_asset
    lucro_real_percent = ((final_amount - volume_a_usar) / volume_a_usar) * 100
    lucro_real_usdt = final_amount - volume_a_usar

    self.stats['trades_executados'] += 1
    self.stats['lucro_total_sessao'] += lucro_real_usdt
    self.bot_data['daily_profit_usdt'] = _dec(self.bot_data.get('daily_profit_usdt', 0)) + lucro_real_usdt
    self.save_config()

    await send_telegram_message(
        "✅ **Arbitragem Executada com Sucesso!**\n" +
        f"Rota: `{' -> '.join(cycle_path)}`\n" +
        f"Volume: `{volume_a_usar:.4f} USDT`\n" +
        f"Lucro: `{lucro_real_usdt:.4f} USDT` (`{lucro_real_percent:.4f}%`)"
    )

==============================================================================

4. Comandos Telegram

==============================================================================

async def send_telegram_message(text: str): if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return try: bot = Bot(token=TELEGRAM_TOKEN)  # type: ignore await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode="Markdown") except Exception as e: logger.error(f"Erro ao enviar mensagem no Telegram: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: help_text = ( "👋 Olá! Sou o Gênesis v17.40, seu bot de arbitragem.\n" "Estou monitorando o mercado 24/7 para encontrar oportunidades.\n" "Use /ajuda para ver a lista de comandos." ) await update.message.reply_text(help_text, parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None: dry_run = bool(context.bot_data.get('dry_run', True)) status_text = "Em operação" if context.bot_data.get('is_running', True) else "Pausado" dry_run_text = "Simulação (Dry Run)" if dry_run else "Modo Real"

response = f"""

🤖 Status do Gênesis v17.40: Status: {status_text} Modo: {dry_run_text} Lucro Mínimo: {_dec(context.bot_data.get('min_profit')):.4f}% Volume de Trade: {_dec(context.bot_data.get('volume_percent')):.2f}% do saldo Profundidade de Rotas: {int(context.bot_data.get('max_depth', MAX_ROUTE_DEPTH_DEFAULT))} Stop Loss (diário): {context.bot_data.get('stop_loss_usdt') or 'Não definido'} USDT

Progresso: {context.bot_data.get('progress_status')} """ await update.message.reply_text(response, parse_mode="Markdown")

async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE): engine: GenesisEngine = context.bot_data.get('engine')  # type: ignore if not engine or not engine.exchange: await update.message.reply_text("Engine não inicializada. Tente novamente mais tarde.") return try: balance = await engine.exchange.fetch_balance() saldo_disponivel = _dec((balance.get('free', {}) or {}).get(MOEDA_BASE_OPERACIONAL, '0')) response = f"📊 Saldo OKX:\n{saldo_disponivel:.4f} {MOEDA_BASE_OPERACIONAL} disponível." await update.message.reply_text(response, parse_mode="Markdown") except Exception as e: await update.message.reply_text(f"❌ Erro ao buscar saldo: {e}")

async def modo_real_command(update: Update, context: ContextTypes.DEFAULT_TYPE): context.bot_data['dry_run'] = False context.bot_data['engine'].save_config()  # type: ignore await update.message.reply_text("✅ Modo Real Ativado!\nO bot agora executará ordens de verdade. Use com cautela.")

async def modo_simulacao_command(update: Update, context: ContextTypes.DEFAULT_TYPE): context.bot_data['dry_run'] = True context.bot_data['engine'].save_config()  # type: ignore await update.message.reply_text("✅ Modo Simulação Ativado!\nO bot apenas simulará trades.")

async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE): try: min_profit = _dec(context.args[0]) if min_profit < 0: raise ValueError context.bot_data['min_profit'] = min_profit context.bot_data['engine'].save_config()  # type: ignore await update.message.reply_text(f"✅ Lucro mínimo definido para {min_profit:.4f}%.") except Exception: await update.message.reply_text("❌ Uso incorreto. Use: /setlucro <porcentagem> (ex: /setlucro 0.1)")

async def setvolume_command(update: Update, context: ContextTypes.DEFAULT_TYPE): try: volume_percent = _dec(context.args[0]) if not (Decimal('0') < volume_percent <= Decimal('100')): raise ValueError context.bot_data['volume_percent'] = volume_percent context.bot_data['engine'].save_config()  # type: ignore await update.message.reply_text(f"✅ Volume de trade definido para {volume_percent:.2f}% do saldo.") except Exception: await update.message.reply_text("❌ Uso incorreto. Use: /setvolume <porcentagem> (ex: /setvolume 50)")

async def pausar_command(update: Update, context: ContextTypes.DEFAULT_TYPE): context.bot_data['is_running'] = False context.bot_data['engine'].save_config()  # type: ignore await update.message.reply_text("⏸️ Motor de arbitragem pausado.")

async def retomar_command(update: Update, context: ContextTypes.DEFAULT_TYPE): context.bot_data['is_running'] = True context.bot_data['engine'].save_config()  # type: ignore await update.message.reply_text("▶️ Motor de arbitragem retomado.")

async def set_stoploss_command(update: Update, context: ContextTypes.DEFAULT_TYPE): try: stop_loss_value = context.args[0].lower() if stop_loss_value == 'off': context.bot_data['stop_loss_usdt'] = None context.bot_data['engine'].save_config()  # type: ignore await update.message.reply_text("✅ Stop Loss desativado.") else: stop_loss = _dec(stop_loss_value) if stop_loss <= 0: raise ValueError # Armazena POSITIVO context.bot_data['stop_loss_usdt'] = stop_loss context.bot_data['engine'].save_config()  # type: ignore await update.message.reply_text(f"✅ Stop Loss (diário) definido para {stop_loss:.2f} USDT.") except Exception: await update.message.reply_text("❌ Uso incorreto. Use: /set_stoploss <valor> (ex: /set_stoploss 100) ou /set_stoploss off.")

async def rotas_command(update: Update, context: ContextTypes.DEFAULT_TYPE): engine: GenesisEngine = context.bot_data.get('engine')  # type: ignore if engine and engine.ecg_data: top_rotas = "\n".join([ f"{i+1}. {' -> '.join(r['cycle'])}\n   Lucro Simulado: {r['profit']:.4f}%" for i, r in enumerate(engine.ecg_data[:5]) ]) response = f"📈 Rotas mais Lucrativas (Simulação):\n\n{top_rotas}" await update.message.reply_text(response, parse_mode="Markdown") return await update.message.reply_text("Ainda não há dados de rotas. O bot pode estar em um ciclo inicial de análise.")

async def ajuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE): help_text = """ 📚 Lista de Comandos: /start - Mensagem de boas-vindas. /status - Mostra o status atual do bot. /saldo - Exibe o saldo disponível em USDT. /modo_real - Ativa o modo de negociação real. /modo_simulacao - Ativa o modo de simulação. /setlucro <%> - Define o lucro mínimo para executar (ex: 0.1). /setvolume <%> - Define a porcentagem do saldo a usar (ex: 50). /pausar - Pausa o motor de arbitragem. /retomar - Retoma o motor. /set_stoploss <valor> - Define stop loss diário em USDT. Use 'off' para desativar. /rotas - Mostra as 5 rotas mais lucrativas simuladas. /ajuda - Exibe esta lista de comandos. /stats - Estatísticas da sessão. /setdepth <n> - Define a profundidade máxima das rotas (padrão: 3, min: 3, max: 5). /progresso - Mostra o status atual do ciclo de análise. """ await update.message.reply_text(help_text, parse_mode="Markdown")

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE): engine: GenesisEngine = context.bot_data.get('engine')  # type: ignore if not engine: await update.message.reply_text("Engine não inicializada.") return stats = engine.stats uptime = time.time() - stats['start_time'] uptime_str = time.strftime("%Hh %Mm %Ss", time.gmtime(uptime))

response = f"""

📊 Estatísticas da Sessão: Tempo de Atividade: {uptime_str} Ciclos de Verificação: {stats['ciclos_verificacao_total']} Rotas Filtradas: {stats['rotas_filtradas']} Trades Executados: {stats['trades_executados']} Lucro Total (Sessão): {stats['lucro_total_sessao']:.4f} {MOEDA_BASE_OPERACIONAL} Erros de Simulação: {stats['erros_simulacao']} """ await update.message.reply_text(response, parse_mode="Markdown")

async def setdepth_command(update: Update, context: ContextTypes.DEFAULT_TYPE): engine: GenesisEngine = context.bot_data.get('engine')  # type: ignore if not engine: await update.message.reply_text("Engine não inicializada.") return try: depth = int(context.args[0]) if not (MIN_ROUTE_DEPTH <= depth <= 5): raise ValueError context.bot_data['max_depth'] = depth await engine.construir_rotas(depth) context.bot_data['engine'].save_config()  # type: ignore await update.message.reply_text(f"✅ Profundidade máxima das rotas definida para {depth}. Rotas recalculadas.") except Exception: await update.message.reply_text(f"❌ Uso incorreto. Use: /setdepth <número> (min: {MIN_ROUTE_DEPTH}, max: 5)")

async def progresso_command(update: Update, context: ContextTypes.DEFAULT_TYPE): status_text = context.bot_data.get('progress_status', 'Status não disponível.') await update.message.reply_text(f"⚙️ Progresso Atual:\n{status_text}")

Pós-init: inicializa engine e dispara loop

async def post_init_tasks(app: Application): logger.info("Iniciando motor Gênesis v17.40 — Revisão completa...") engine = GenesisEngine(app) app.bot_data['engine'] = engine await send_telegram_message("🤖 Gênesis v17.40 iniciado.\nAs configurações são salvas/carregadas automaticamente.") if await engine.inicializar_exchange(): await engine.construir_rotas(app.bot_data.get('max_depth', MAX_ROUTE_DEPTH_DEFAULT)) asyncio.create_task(engine.verificar_oportunidades()) logger.info("Motor e tarefas de fundo iniciadas.") else: await send_telegram_message("❌ ERRO CRÍTICO: Não foi possível conectar à OKX.") if engine.exchange: await engine.exchange.close()

==============================================================================

5. Main

==============================================================================

def main(): if not TELEGRAM_TOKEN: logger.critical("Token do Telegram não encontrado.") return application = Application.builder().token(TELEGRAM_TOKEN).build()

command_map = {
    "start": start_command,
    "status": status_command,
    "saldo": saldo_command,
    "modo_real": modo_real_command,
    "modo_simulacao": modo_simulacao_command,
    "setlucro": setlucro_command,
    "setvolume": setvolume_command,
    "pausar": pausar_command,
    "retomar": retomar_command,
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

