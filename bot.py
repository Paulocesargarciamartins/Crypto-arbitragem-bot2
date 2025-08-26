# bot_okx_fixed.py
# Gênesis v17.9 (OKX) - Versão corrigida completa (stop-loss imediato 1%)
import os
import asyncio
import logging
from decimal import Decimal, getcontext, ROUND_DOWN
import time
import uuid
import sys
import signal

import ccxt.async_support as ccxt
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_helper import ApiTelegramException

# --------------- CONFIG ---------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_ID_RAW = os.getenv("TELEGRAM_CHAT_ID")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD")

TAXA_OPERACAO = Decimal("0.001")  # 0.1% taker fee aproximado
MIN_PROFIT_DEFAULT = Decimal("0.001")
MARGEM_DE_SEGURANCA = Decimal("0.995")
MOEDAS_BASE_OPERACIONAL = ["USDT", "USDC"]
MAX_ROUTE_DEPTH = 4
ORDER_BOOK_DEPTH = 100

# Stop loss thresholds (percentagens negativas)
STOP_LOSS_LEVEL_WARN = Decimal("-0.5")   # alerta
STOP_LOSS_LEVEL_CRIT = Decimal("-1.0")   # venda de pânico (1% perda)

getcontext().prec = 30

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger("GenesisOKX")

# --------------- HELPERS ---------------
def _safe_decimal(value):
    try:
        if value is None or value == '':
            return None
        return Decimal(str(value))
    except Exception:
        return None

def _quantizer_from_decimal(d: Decimal, fallback_places: int = 8):
    if not d:
        return Decimal(f"1e-{fallback_places}")
    places = max(-d.as_tuple().exponent, 0)
    return Decimal("1").scaleb(-places)

def parse_balances(balance_response):
    """
    Normaliza fetch_balance() do ccxt para dict currency -> Decimal(free)
    """
    try:
        if not isinstance(balance_response, dict):
            return {}
        free = balance_response.get('free') or {}
        return {c: Decimal(str(v)) if v is not None else Decimal('0') for c, v in free.items()}
    except Exception as e:
        logger.error(f"Erro parseando saldos: {e}", exc_info=True)
        return {}

# --------------- OKX API CLIENT ---------------
class OKXApiClient:
    def __init__(self, api_key, secret_key, password):
        self.exchange = ccxt.okx({
            'apiKey': api_key,
            'secret': secret_key,
            'password': password,
            'enableRateLimit': True,
            'options': {'defaultType': 'spot', 'adjustForTimeDifference': True}
        })
        self.markets = {}
        self.closed = False

    async def load_markets(self):
        try:
            self.markets = await self.exchange.load_markets(True)
            return self.markets
        except Exception as e:
            logger.error(f"Erro ao carregar mercados: {e}", exc_info=True)
            return None

    async def _execute_api_call(self, api_call, *args, **kwargs):
        try:
            return await api_call(*args, **kwargs)
        except ccxt.ExchangeError as ex:
            logger.error(f"CCXT ExchangeError: {ex}", exc_info=True)
            return {'error': str(ex), 'exception': ex}
        except Exception as e:
            logger.error(f"Unknown API error: {e}", exc_info=True)
            return {'error': str(e)}

    async def get_all_pairs(self):
        return self.markets

    async def get_spot_balances(self):
        return await self._execute_api_call(self.exchange.fetch_balance)

    async def create_market_buy_order(self, symbol, amount):
        # amount = quantidade base (ex: BTC) a comprar
        return await self._execute_api_call(self.exchange.create_market_buy_order, symbol, amount)

    async def create_market_sell_order(self, symbol, amount):
        # amount = quantidade base (ex: BTC) a vender
        return await self._execute_api_call(self.exchange.create_market_sell_order, symbol, amount)

    async def get_order_book(self, symbol):
        return await self._execute_api_call(self.exchange.fetch_order_book, symbol, ORDER_BOOK_DEPTH)

    async def get_ticker(self, symbol):
        return await self._execute_api_call(self.exchange.fetch_ticker, symbol)

    async def close(self):
        if not self.closed:
            try:
                await self.exchange.close()
            except Exception:
                pass
            self.closed = True

# --------------- ENGINE ---------------
class GenesisEngine:
    def __init__(self, bot_instance: AsyncTeleBot, api_client: OKXApiClient):
        self.bot = bot_instance
        self.api_client = api_client
        self.bot_data = {
            "is_running": True,
            "min_profit": MIN_PROFIT_DEFAULT,
            "dry_run": True,
            "max_route_depth": MAX_ROUTE_DEPTH
        }
        self.pair_rules = {}  # symbol -> market info
        self.graph = {}       # adjacency by currency
        self.rotas_monitoradas = []
        self.simulacao_data = []
        self.trade_lock = asyncio.Lock()
        self.stats = {
            "start_time": time.time(),
            "ciclos_verificacao_total": 0,
            "rotas_sobreviventes_total": 0,
            "ultimo_ciclo_timestamp": time.time()
        }
        self.stop_loss_monitoring_task = None

    async def inicializar(self):
        logger.info("Gênesis v17.9 (OKX): Iniciando carregamento de mercados...")
        all_pairs = await self.api_client.load_markets()
        if not all_pairs:
            logger.critical("Não foi possível carregar mercados da OKX.")
            return

        # construir pair_rules e grafo
        self.pair_rules = {}
        self.graph = {}
        market_list = list(all_pairs.items())
        chunk_size = max(1, len(market_list) // 3)

        for i in range(0, len(market_list), chunk_size):
            chunk = market_list[i:i + chunk_size]
            for pair_id, pair_data in chunk:
                try:
                    active = pair_data.get('active', True)
                    if not active:
                        continue
                    base = pair_data.get('base')
                    quote = pair_data.get('quote')
                    if not base or not quote:
                        continue
                    self.pair_rules[pair_id] = pair_data
                    self.graph.setdefault(base, []).append(quote)
                    self.graph.setdefault(quote, []).append(base)
                except Exception as e:
                    logger.warning(f"Erro processando pair {pair_id}: {e}")
            await asyncio.sleep(0.2)

        logger.info(f"Mapa construído. Gerando rotas até {self.bot_data['max_route_depth']} passos...")
        self.rotas_monitoradas = []
        for start in MOEDAS_BASE_OPERACIONAL:
            if start not in self.graph:
                continue
            def dfs(u, path, depth):
                if depth > self.bot_data["max_route_depth"]:
                    return
                for v in self.graph.get(u, []):
                    if v == start and len(path) > 2:
                        self.rotas_monitoradas.append(path + [v])
                    elif v not in path:
                        dfs(v, path + [v], depth + 1)
            dfs(start, [start], 1)

        # dedupe
        self.rotas_monitoradas = [list(x) for x in {tuple(r) for r in self.rotas_monitoradas}]
        logger.info(f"{len(self.rotas_monitoradas)} rotas únicas encontradas.")
        self.bot_data["total_ciclos"] = len(self.rotas_monitoradas)

    def _get_pair_details(self, coin_from, coin_to):
        pair_v1 = f"{coin_from}/{coin_to}"
        if pair_v1 in self.pair_rules:
            return pair_v1, "sell"  # tenho par na forma base=coin_from -> posso vender base para receber quote
        pair_v2 = f"{coin_to}/{coin_from}"
        if pair_v2 in self.pair_rules:
            return pair_v2, "buy"   # par na forma base=coin_to -> para trocar coin_from para coin_to eu compro base (coin_to)
        return None, None

    async def _simular_realidade(self, cycle_path, investimento_inicial):
        try:
            valor_simulado = Decimal(investimento_inicial)
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i+1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)
                if not pair_id:
                    return None
                pair_info = self.pair_rules.get(pair_id)
                if not pair_info:
                    return None

                amount_prec = pair_info.get('precision', {}).get('amount', 8)
                quantizer = Decimal(f"1e-{amount_prec}")

                order_book = await self.api_client.get_order_book(pair_id)
                if not order_book or isinstance(order_book, dict) and order_book.get('error'):
                    return None

                if side == "buy":
                    # valor_simulado is amount in quote (coin_from) to spend
                    valor_a_gastar = Decimal(valor_simulado)
                    quantidade_comprada = Decimal("0")
                    asks = order_book.get('asks') or []
                    for preco_str, quantidade_str in asks:
                        preco, quantidade_disponivel = Decimal(str(preco_str)), Decimal(str(quantidade_str))
                        custo_nivel = preco * quantidade_disponivel
                        if valor_a_gastar >= custo_nivel:
                            quantidade_comprada += quantidade_disponivel
                            valor_a_gastar -= custo_nivel
                        else:
                            if preco == 0:
                                break
                            qtd_a_comprar = (valor_a_gastar / preco).quantize(quantizer, rounding=ROUND_DOWN)
                            if qtd_a_comprar <= 0:
                                break
                            quantidade_comprada += qtd_a_comprar
                            valor_a_gastar = Decimal("0")
                            break
                    if valor_a_gastar > 0:
                        return None
                    if 'limits' in pair_info and pair_info['limits'].get('amount') and pair_info['limits']['amount'].get('min'):
                        if quantidade_comprada < Decimal(str(pair_info['limits']['amount']['min'])):
                            return None
                    valor_simulado = quantidade_comprada
                else:
                    # sell: valor_simulado is amount in base (coin_from) to sell
                    quantidade_a_vender = Decimal(valor_simulado).quantize(quantizer, rounding=ROUND_DOWN)
                    if quantidade_a_vender <= 0:
                        return None
                    valor_recebido = Decimal("0")
                    bids = order_book.get('bids') or []
                    for preco_str, quantidade_str in bids:
                        preco, quantidade_disponivel = Decimal(str(preco_str)), Decimal(str(quantidade_str))
                        if quantidade_a_vender > quantidade_disponivel:
                            valor_recebido += quantidade_disponivel * preco
                            quantidade_a_vender -= quantidade_disponivel
                        else:
                            valor_recebido += quantidade_a_vender * preco
                            quantidade_a_vender = Decimal("0")
                            break
                    if quantidade_a_vender > 0:
                        return None
                    if 'limits' in pair_info and pair_info['limits'].get('amount') and pair_info['limits']['amount'].get('min'):
                        if Decimal(str(valor_simulado)) < Decimal(str(pair_info['limits']['amount']['min'])):
                            return None
                    valor_simulado = valor_recebido

                # aplicar taxa
                valor_simulado = (valor_simulado * (Decimal("1") - TAXA_OPERACAO)).quantize(Decimal('0.00000001'))
                await asyncio.sleep(0.001)

            if Decimal(investimento_inicial) == 0:
                return Decimal("0")
            return ((valor_simulado - Decimal(investimento_inicial)) / Decimal(investimento_inicial)) * 100
        except Exception as e:
            rota_str = " -> ".join(cycle_path)
            logger.error(f"Erro na simulação para a rota {rota_str}: {e}", exc_info=True)
            return None

    async def verificar_oportunidades(self):
        logger.info("Gênesis: Motor de verificação iniciado.")
        while True:
            try:
                if not self.bot_data.get("is_running", True) or self.trade_lock.locked():
                    await asyncio.sleep(1)
                    continue

                self.stats["ciclos_verificacao_total"] += 1
                self.stats["ultimo_ciclo_timestamp"] = time.time()

                saldos_resp = await self.api_client.get_spot_balances()
                if not isinstance(saldos_resp, dict) or saldos_resp.get('error'):
                    await asyncio.sleep(5)
                    continue
                saldo_por_moeda = parse_balances(saldos_resp)

                self.simulacao_data = []
                for cycle_path in self.rotas_monitoradas:
                    moeda_inicial_rota = cycle_path[0]
                    volume_a_simular = saldo_por_moeda.get(moeda_inicial_rota, Decimal("0"))
                    if volume_a_simular > 0:
                        profit = await self._simular_realidade(cycle_path, volume_a_simular)
                        if profit is not None:
                            self.simulacao_data.append({"cycle": cycle_path, "profit": profit, "investment": volume_a_simular})
                    await asyncio.sleep(0.001)

                oportunidades_reais = sorted([op for op in self.simulacao_data if op["profit"] > self.bot_data["min_profit"]],
                                            key=lambda x: x["profit"], reverse=True)
                self.stats["rotas_sobreviventes_total"] += len(oportunidades_reais)

                if oportunidades_reais:
                    async with self.trade_lock:
                        melhor = oportunidades_reais[0]
                        rota_str = " -> ".join(melhor["cycle"])
                        logger.info(f"Oportunidade REALISTA encontrada ({melhor['profit']:.4f}%) rota {rota_str}.")
                        # executar (respeita dry_run)
                        await self._executar_trade_realista(melhor["cycle"], melhor["investment"])
            except Exception as e:
                logger.error(f"Erro no loop principal: {e}", exc_info=True)
            finally:
                await asyncio.sleep(10)

    async def _convert_to_preferred_base(self, amount: Decimal, currency: str, preferred_base: str = "USDT"):
        """
        Tenta converter `amount` na `currency` para o valor equivalente em preferred_base (USDT/USDC).
        Retorna Decimal valor em preferred_base ou None.
        """
        try:
            if currency == preferred_base:
                return Decimal(amount)
            cand1 = f"{currency}/{preferred_base}"
            cand2 = f"{preferred_base}/{currency}"
            if cand1 in self.pair_rules:
                ticker = await self.api_client.get_ticker(cand1)
                if not ticker or ticker.get('error'):
                    return None
                price = Decimal(str(ticker.get('last', 0)))
                return (amount * price).quantize(Decimal('0.00000001'))
            elif cand2 in self.pair_rules:
                ticker = await self.api_client.get_ticker(cand2)
                if not ticker or ticker.get('error'):
                    return None
                price = Decimal(str(ticker.get('last', 0)))
                if price == 0:
                    return None
                return (amount / price).quantize(Decimal('0.00000001'))
            else:
                # fallback: try both USDT and USDC via markets lookup
                for base_try in MOEDAS_BASE_OPERACIONAL:
                    cand = f"{currency}/{base_try}"
                    if cand in self.pair_rules:
                        ticker = await self.api_client.get_ticker(cand)
                        if not ticker or ticker.get('error'):
                            continue
                        price = Decimal(str(ticker.get('last', 0)))
                        return (amount * price).quantize(Decimal('0.00000001'))
            return None
        except Exception as e:
            logger.error(f"Erro converting to preferred base: {e}", exc_info=True)
            return None

    async def _monitorar_stop_loss(self, moeda_destino: str, investimento_inicial_em_base: Decimal, moeda_base_pref: str = "USDT"):
        """
        Monitora imediatamente após a primeira ordem bem-sucedida.
        investimento_inicial_em_base: valor do investimento convertido para moeda_base_pref (ex: USDT)
        """
        logger.info(f"Monitoramento de stop-loss iniciado para {moeda_destino} (base {moeda_base_pref}).")
        try:
            await self.bot.send_message(ADMIN_CHAT_ID, (
                f"⚠️ *Monitoramento de Stop-Loss Ativado!* \n"
                f"Ativo: `{moeda_destino}`\n"
                f"Referência (em {moeda_base_pref}): `{investimento_inicial_em_base}`\n"
                f"Critico (venda): {STOP_LOSS_LEVEL_CRIT}%\n"
            ), parse_mode="Markdown")
            last_warn = False

            while True:
                saldos_resp = await self.api_client.get_spot_balances()
                if not isinstance(saldos_resp, dict) or saldos_resp.get('error'):
                    await asyncio.sleep(2)
                    continue
                free_balances = parse_balances(saldos_resp)
                saldo_atual_unidade = free_balances.get(moeda_destino, Decimal("0"))
                if saldo_atual_unidade <= 0:
                    logger.info("Saldo da moeda destino zerado, finalizando monitoramento de stop-loss.")
                    break

                # determinar par para converter moeda_destino -> moeda_base_pref
                cand1 = f"{moeda_destino}/{moeda_base_pref}"
                cand2 = f"{moeda_base_pref}/{moeda_destino}"
                invert = False
                pair_for_price = None
                if cand1 in self.pair_rules:
                    pair_for_price = cand1
                    invert = False
                elif cand2 in self.pair_rules:
                    pair_for_price = cand2
                    invert = True
                else:
                    # fallback: try any base (USDT/USDC)
                    for base_try in MOEDAS_BASE_OPERACIONAL:
                        cand_try = f"{moeda_destino}/{base_try}"
                        if cand_try in self.pair_rules:
                            pair_for_price = cand_try
                            invert = False
                            moeda_base_pref = base_try
                            break
                        cand_try2 = f"{base_try}/{moeda_destino}"
                        if cand_try2 in self.pair_rules:
                            pair_for_price = cand_try2
                            invert = True
                            moeda_base_pref = base_try
                            break

                if not pair_for_price:
                    await asyncio.sleep(2)
                    continue

                ticker = await self.api_client.get_ticker(pair_for_price)
                if not ticker or ticker.get('error'):
                    await asyncio.sleep(2)
                    continue
                last_price = Decimal(str(ticker.get('last', 0)))
                if last_price == 0:
                    await asyncio.sleep(2)
                    continue

                if invert:
                    preco_conversao = (Decimal("1") / last_price).quantize(Decimal('0.00000001'))
                else:
                    preco_conversao = last_price

                valor_atual_em_base = (saldo_atual_unidade * preco_conversao).quantize(Decimal('0.00000001'))

                perda_percentual = ((valor_atual_em_base - investimento_inicial_em_base) / investimento_inicial_em_base) * 100

                if perda_percentual < STOP_LOSS_LEVEL_CRIT:
                    # venda de pânico
                    await self.bot.send_message(ADMIN_CHAT_ID, (
                        f"🛑 *STOP-LOSS CRÍTICO!* \n"
                        f"{moeda_destino} - Perda: `{perda_percentual:.2f}%`. Tentando vender todo o saldo."
                    ), parse_mode="Markdown")
                    # escolher par de venda preferencial
                    sell_pair = None
                    if cand1 in self.pair_rules:
                        sell_pair = cand1
                    elif cand2 in self.pair_rules:
                        sell_pair = cand2
                    else:
                        # tenta pegar par moeda_destino/preferida
                        for base_try in MOEDAS_BASE_OPERACIONAL:
                            p = f"{moeda_destino}/{base_try}"
                            if p in self.pair_rules:
                                sell_pair = p
                                break
                    if sell_pair:
                        # vender quantidade atual (base)
                        await self.api_client.create_market_sell_order(sell_pair, float(saldo_atual_unidade))
                        await self.bot.send_message(ADMIN_CHAT_ID, "✅ Venda de pânico enviada.", parse_mode="Markdown")
                    else:
                        await self.bot.send_message(ADMIN_CHAT_ID, "❌ Não encontrou par de venda para liquidação automática.", parse_mode="Markdown")
                    break
                elif perda_percentual < STOP_LOSS_LEVEL_WARN and not last_warn:
                    last_warn = True
                    await self.bot.send_message(ADMIN_CHAT_ID, (
                        f"🚨 *ALERTA STOP-LOSS* \n"
                        f"{moeda_destino} - Perda: `{perda_percentual:.2f}%`. Próximo nível: {STOP_LOSS_LEVEL_CRIT}%"
                    ), parse_mode="Markdown")
                await asyncio.sleep(3)
        except Exception as e:
            logger.error(f"Erro no monitoramento de stop-loss: {e}", exc_info=True)
            await self.bot.send_message(ADMIN_CHAT_ID, f"❌ Erro no monitoramento de stop-loss: `{e}`", parse_mode="Markdown")
        finally:
            logger.info("Monitoramento de stop-loss finalizado.")

    async def _compute_amount_base_for_buy(self, pair_id, budget_in_quote):
        """
        Dado pair_id na forma base/quote, e budget em quote, computa quantos base podem ser comprados com o budget
        usando o orderbook (asks).
        Retorna Decimal quantidade base ou None
        """
        try:
            order_book = await self.api_client.get_order_book(pair_id)
            if not order_book or order_book.get('error'):
                return None
            asks = order_book.get('asks') or []
            remaining = Decimal(budget_in_quote)
            bought = Decimal("0")
            for price_s, amount_s in asks:
                price = Decimal(str(price_s))
                avail = Decimal(str(amount_s))
                cost_level = (price * avail).quantize(Decimal('0.00000001'))
                if remaining >= cost_level:
                    bought += avail
                    remaining -= cost_level
                else:
                    if price == 0:
                        break
                    qty = (remaining / price).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
                    if qty <= 0:
                        break
                    bought += qty
                    remaining = Decimal("0")
                    break
            if remaining > 0:
                return None
            return bought
        except Exception as e:
            logger.error(f"Erro compute_amount_base_for_buy: {e}", exc_info=True)
            return None

    async def _executar_trade_realista(self, cycle_path, investimento_inicial_volume):
        """
        Executa rota (respeita dry_run). Inicia stop-loss IMEDIATAMENTE após a primeira ordem bem sucedida.
        investimento_inicial_volume: quantidade de moeda inicial (em moeda_inicial_rota)
        """
        is_dry_run = self.bot_data.get("dry_run", True)
        rota_str = " -> ".join(cycle_path)
        moeda_inicial_rota = cycle_path[0]
        investimento_inicial = Decimal(investimento_inicial_volume)

        if is_dry_run:
            profit_rota = next((x["profit"] for x in self.simulacao_data if x["cycle"] == cycle_path), None)
            await self.bot.send_message(ADMIN_CHAT_ID, (
                f"🎯 *Alvo Realista (Simulação)*\n"
                f"Rota: `{rota_str}`\n"
                f"Investimento: `{investimento_inicial:.8f} {moeda_inicial_rota}`\n"
                f"Lucro previsto: `{(profit_rota if profit_rota is not None else Decimal('0')):.4f}%`"
            ), parse_mode="Markdown")
            return

        await self.bot.send_message(ADMIN_CHAT_ID, (
            f"🚀 *Iniciando Trade REAL...*\n"
            f"Rota: `{rota_str}`\n"
            f"Investimento planejado: `{investimento_inicial:.8f} {moeda_inicial_rota}`"
        ), parse_mode="Markdown")

        # iremos usar async with trade_lock no chamador (verificar)
        first_order_done = False
        investimento_inicial_em_base = None  # em USDT/USDC referência para stop-loss

        try:
            for i in range(len(cycle_path) - 1):
                coin_from, coin_to = cycle_path[i], cycle_path[i + 1]
                pair_id, side = self._get_pair_details(coin_from, coin_to)

                # checar saldo real no momento
                saldos_resp = await self.api_client.get_spot_balances()
                if not isinstance(saldos_resp, dict) or saldos_resp.get('error'):
                    await self.bot.send_message(ADMIN_CHAT_ID, "❌ Erro lendo saldos antes do passo. Abortando trade.", parse_mode="Markdown")
                    return
                free_balances = parse_balances(saldos_resp)
                saldo_a_negociar = free_balances.get(coin_from, Decimal("0"))

                if saldo_a_negociar <= 0:
                    await self.bot.send_message(ADMIN_CHAT_ID, (
                        f"❌ *FALHA CRÍTICA (Passo {i+1})*\n"
                        f"Saldo de `{coin_from}` é zero. Abortando."
                    ), parse_mode="Markdown")
                    return

                pair_info = self.pair_rules.get(pair_id)
                if not pair_info:
                    await self.bot.send_message(ADMIN_CHAT_ID, (
                        f"❌ *FALHA CRÍTICA (Passo {i+1})*\n"
                        f"Par `{pair_id}` não encontrado nas regras. Abortando."
                    ), parse_mode="Markdown")
                    return

                amount_prec = pair_info.get('precision', {}).get('amount', 8)
                quantizer = Decimal(f"1e-{amount_prec}")

                amount_to_trade = (saldo_a_negociar * MARGEM_DE_SEGURANCA).quantize(quantizer, rounding=ROUND_DOWN)

                if amount_to_trade <= 0:
                    await self.bot.send_message(ADMIN_CHAT_ID, (
                        f"❌ *FALHA CRÍTICA (Passo {i+1})*\n"
                        f"Quantidade a negociar (`{amount_to_trade}`) insuficiente. Abortando."
                    ), parse_mode="Markdown")
                    return

                if pair_info.get('limits', {}).get('amount') and pair_info['limits']['amount'].get('min'):
                    if amount_to_trade < Decimal(str(pair_info['limits']['amount']['min'])):
                        await self.bot.send_message(ADMIN_CHAT_ID, (
                            f"⚠️ Passo {i+1}: amount ({amount_to_trade}) abaixo do mínimo do par ({pair_info['limits']['amount']['min']}). Abortando."
                        ), parse_mode="Markdown")
                        return

                await self.bot.send_message(ADMIN_CHAT_ID, (
                    f"⏳ Passo {i+1}/{len(cycle_path)-1}: Negociando `{amount_to_trade} {coin_from}` -> `{coin_to}` no par `{pair_id}`"
                ), parse_mode="Markdown")

                # EXECUTAR ordem de mercado com cuidado: se side == 'buy' precisamos calcular quantidade base
                if side == 'buy':
                    # pair_id é base/quote com base = coin_to, quote = coin_from
                    budget_in_quote = amount_to_trade  # em coin_from (quote)
                    quantidade_base = await self._compute_amount_base_for_buy(pair_id, budget_in_quote)
                    if quantidade_base is None or quantidade_base <= 0:
                        await self.bot.send_message(ADMIN_CHAT_ID, (
                            f"❌ Não foi possível determinar quantidade a comprar para {pair_id} com budget {budget_in_quote}. Abortando."
                        ), parse_mode="Markdown")
                        return
                    order_result = await self.api_client.create_market_buy_order(pair_id, float(quantidade_base))
                else:
                    # sell: pair_id é coin_from/coin_to (base=coin_from)
                    quantidade_base_sell = amount_to_trade  # já é a quantidade em base
                    order_result = await self.api_client.create_market_sell_order(pair_id, float(quantidade_base_sell))

                if isinstance(order_result, dict) and order_result.get('error'):
                    # erro de exchange
                    err_msg = order_result.get('error')
                    await self.bot.send_message(ADMIN_CHAT_ID, (
                        f"❌ *FALHA NO PASSO {i+1} ({pair_id})*\nMotivo: `{err_msg}`"
                    ), parse_mode="Markdown")
                    # se falha no primeiro passo, iniciamos monitoramento da moeda destino (proteção) também
                    if not first_order_done:
                        # converter investimento inicial para USDT/USDC de referência
                        pref = "USDT" if "USDT" in MOEDAS_BASE_OPERACIONAL else MOEDAS_BASE_OPERACIONAL[0]
                        conv = await self._convert_to_preferred_base(investimento_inicial, moeda_inicial_rota, pref)
                        if conv:
                            if not self.stop_loss_monitoring_task or self.stop_loss_monitoring_task.done():
                                self.stop_loss_monitoring_task = asyncio.create_task(self._monitorar_stop_loss(coin_to, conv, pref))
                    return

                # se ordens sucederam:
                if not first_order_done:
                    # iniciar proteção IMEDIATA após a primeira ordem bem sucedida
                    pref = "USDT" if "USDT" in MOEDAS_BASE_OPERACIONAL else MOEDAS_BASE_OPERACIONAL[0]
                    conv = await self._convert_to_preferred_base(investimento_inicial, moeda_inicial_rota, pref)
                    if conv:
                        investimento_inicial_em_base = conv
                        # iniciar monitor de stop-loss
                        if not self.stop_loss_monitoring_task or self.stop_loss_monitoring_task.done():
                            self.stop_loss_monitoring_task = asyncio.create_task(self._monitorar_stop_loss(coin_to, investimento_inicial_em_base, pref))
                    first_order_done = True

                await asyncio.sleep(1)  # pausa pequena entre passos

            await self.bot.send_message(ADMIN_CHAT_ID, "✅ Trade concluído com sucesso!", parse_mode="Markdown")

        except Exception as e:
            logger.error(f"Erro durante execução do trade: {e}", exc_info=True)
            await self.bot.send_message(ADMIN_CHAT_ID, f"❌ Erro crítico durante o trade: `{e}`", parse_mode="Markdown")
        finally:
            # NÃO CANCELAR stop_loss_monitoring_task aqui (deixamos rodando)
            logger.info(f"Finalizado execução da rota {rota_str}.")

    async def gerar_relatorio_detalhado(self, cycle_path: list):
        return "⚠️ Função de relatório detalhado não implementada nesta versão."

# --------------- TELEGRAM INTERFACE ---------------
# Note: AsyncTeleBot uses decorators; we'll attach handlers programmatically.

async def start_command(message):
    await bot.reply_to(message, "Olá! Gênesis v17.9 (OKX) online. Use /status para começar.")

async def status_command(message):
    engine = bot.engine
    status_text = "▶️ Rodando" if engine.bot_data.get('is_running') else "⏸️ Pausado"
    if engine.bot_data.get('is_running') and engine.trade_lock.locked():
        status_text = "▶️ Rodando (Processando Alvo)"
    msg = (f"*📊 Painel de Controle - Gênesis v17.9 (OKX)*\n\n"
           f"*Estado:* `{status_text}`\n"
           f"*Modo:* `{'Simulação' if engine.bot_data.get('dry_run') else 'REAL'}`\n"
           f"*Lucro Mínimo (Líquido Realista):* `{engine.bot_data.get('min_profit')}%`\n"
           f"*Profundidade de Busca:* `{engine.bot_data.get('max_route_depth')}`\n"
           f"*Total de Rotas Monitoradas:* `{engine.bot_data.get('total_ciclos', 0)}`")
    await bot.send_message(message.chat.id, msg, parse_mode='Markdown')

async def radar_command(message):
    engine: GenesisEngine = bot.engine
    if not engine or not engine.simulacao_data:
        await bot.reply_to(message, "📡 Radar do Caçador (OKX): Nenhuma simulação concluída ainda.")
        return
    oportunidades_reais = sorted([op for op in engine.simulacao_data if op['profit'] > 0], key=lambda x: x['profit'], reverse=True)
    if not oportunidades_reais:
        await bot.reply_to(message, "🔎 Nenhuma oportunidade de lucro acima de 0% encontrada no momento.")
        return
    top_5 = oportunidades_reais[:5]
    msg = "📡 *Radar do Caçador (Top 5 - OKX)*\n\n"
    for r in top_5:
        rota_fmt = ' -> '.join(r['cycle'])
        msg += f"- Rota: `{rota_fmt}`\n  Lucro: `🔼 {r['profit']:.4f}%`\n\n"
    await bot.send_message(message.chat.id, msg, parse_mode='Markdown')

async def debug_radar_command(message):
    await bot.reply_to(message, "⚠️ Função de relatório detalhado não implementada nesta versão.")

async def diagnostico_command(message):
    engine: GenesisEngine = bot.engine
    if not engine:
        await bot.reply_to(message, "Motor ainda não inicializado.")
        return
    uptime_seconds = time.time() - engine.stats['start_time']
    m, s = divmod(uptime_seconds, 60)
    h, m = divmod(m, 60)
    uptime_str = f"{int(h)}h {int(m)}m {int(s)}s"
    tempo_desde_ultimo = time.time() - engine.stats['ultimo_ciclo_timestamp']
    msg = (f"*🩺 Diagnóstico Interno - Gênesis v17.9 (OKX)*\n\n"
           f"*Ativo há:* `{uptime_str}`\n"
           f"*Motor Principal:* `{'ATIVO' if engine.bot_data.get('is_running') else 'PAUSADO'}`\n"
           f"*Trava de Trade:* `{'BLOQUEADO' if engine.trade_lock.locked() else 'LIVRE'}`\n"
           f"*Último Ciclo:* `{tempo_desde_ultimo:.1f}s atrás`\n\n"
           f"--- *Estatísticas da Sessão* ---\n"
           f"Ciclos verificação: `{engine.stats['ciclos_verificacao_total']}`\n"
           f"Rotas sobreviventes (sim): `{engine.stats['rotas_sobreviventes_total']}`\n")
    await bot.send_message(message.chat.id, msg, parse_mode='Markdown')

async def saldo_command(message):
    engine: GenesisEngine = bot.engine
    if not engine:
        await bot.reply_to(message, "Conexão com exchange não estabelecida.")
        return
    await bot.reply_to(message, "Buscando saldos na OKX...")
    try:
        saldos = await engine.api_client.get_spot_balances()
        if not isinstance(saldos, dict) or saldos.get('error'):
            await bot.reply_to(message, f"❌ Erro ao buscar saldos: {saldos.get('error') if isinstance(saldos, dict) else 'Resposta inválida'}")
            return
        free_bal = parse_balances(saldos)
        non_zero = {c: v for c, v in free_bal.items() if v > 0}
        if not non_zero:
            await bot.reply_to(message, "Nenhum saldo encontrado.")
            return
        msg = "*💰 Saldos Atuais (Spot OKX)*\n\n"
        for moeda, saldo in non_zero.items():
            msg += f"*{moeda}:* `{saldo}`\n"
        await bot.send_message(message.chat.id, msg, parse_mode='Markdown')
    except Exception as e:
        await bot.reply_to(message, f"❌ Erro ao buscar saldos: `{e}`")

async def modo_real_command(message):
    bot.engine.bot_data['dry_run'] = False
    await bot.reply_to(message, "🔴 *MODO REAL ATIVADO (OKX).*", parse_mode='Markdown')
    await status_command(message)

async def modo_simulacao_command(message):
    bot.engine.bot_data['dry_run'] = True
    await bot.reply_to(message, "🔵 *Modo Simulação Ativado (OKX).*", parse_mode='Markdown')
    await status_command(message)

async def setlucro_command(message):
    try:
        val = message.text.split()[1]
        bot.engine.bot_data['min_profit'] = Decimal(val)
        await bot.reply_to(message, f"✅ Lucro mínimo definido para *{val}%*.", parse_mode='Markdown')
    except Exception:
        await bot.reply_to(message, "⚠️ Uso: `/setlucro 0.01`")

async def setdepth_command(message):
    try:
        new_depth = int(message.text.split()[1])
        if 2 <= new_depth <= 6:
            bot.engine.bot_data['max_route_depth'] = new_depth
            await bot.reply_to(message, f"✅ Profundidade de busca definida para *{new_depth}*. Reconstruindo rotas...", parse_mode='Markdown')
            await bot.engine.inicializar()
        else:
            await bot.reply_to(message, "⚠️ Profundidade entre 2 e 6.")
    except Exception:
        await bot.reply_to(message, "⚠️ Uso: `/setdepth 4`")

async def pausar_command(message):
    bot.engine.bot_data['is_running'] = False
    await bot.reply_to(message, "⏸️ *Bot pausado.*", parse_mode='Markdown')
    await status_command(message)

async def retomar_command(message):
    bot.engine.bot_data['is_running'] = True
    await bot.reply_to(message, "✅ *Bot retomado.*", parse_mode='Markdown')
    await status_command(message)

# --------------- MAIN ---------------
async def main():
    # validar env
    missing = [k for k in ("OKX_API_KEY", "OKX_API_SECRET", "OKX_API_PASSWORD", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID") if not os.getenv(k)]
    if missing:
        logger.critical(f"Variáveis de ambiente incompletas: {missing}")
        return

    global ADMIN_CHAT_ID
    try:
        ADMIN_CHAT_ID = int(ADMIN_CHAT_ID_RAW)
    except Exception:
        logger.critical("ADMIN_CHAT_ID inválido. Deve ser um inteiro (id do chat).")
        return

    global bot
    bot = AsyncTeleBot(TELEGRAM_TOKEN)

    # registrar handlers
    bot.message_handler(commands=['start'])(start_command)
    bot.message_handler(commands=['status'])(status_command)
    bot.message_handler(commands=['radar'])(radar_command)
    bot.message_handler(commands=['debug_radar'])(debug_radar_command)
    bot.message_handler(commands=['diagnostico'])(diagnostico_command)
    bot.message_handler(commands=['saldo'])(saldo_command)
    bot.message_handler(commands=['modo_real'])(modo_real_command)
    bot.message_handler(commands=['modo_simulacao'])(modo_simulacao_command)
    bot.message_handler(commands=['setlucro'])(setlucro_command)
    bot.message_handler(commands=['setdepth'])(setdepth_command)
    bot.message_handler(commands=['pausar'])(pausar_command)
    bot.message_handler(commands=['retomar'])(retomar_command)

    logger.info("Iniciando motor Gênesis v17.9 (OKX)...")
    try:
        await bot.send_message(ADMIN_CHAT_ID, "🤖 Gênesis v17.9 (OKX) iniciado. Carregando dados...")
    except ApiTelegramException as e:
        logger.critical(f"Falha ao enviar mensagem inicial: {e}")
        sys.exit(1)

    api_client = OKXApiClient(OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSWORD)
    bot.engine = GenesisEngine(bot, api_client)
    await bot.engine.inicializar()

    # task de verificação
    verificar_task = asyncio.create_task(bot.engine.verificar_oportunidades())

    # polling
    logger.info("Iniciando polling do Telebot...")
    try:
        await bot.polling()
    finally:
        logger.info("Shutdown iniciado. Cancelando tasks e fechando conexões...")
        verificar_task.cancel()
        try:
            await api_client.close()
        except Exception:
            pass

# lidar com SIGINT/SIGTERM em asyncio run
def _run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Encerramento por KeyboardInterrupt.")
    except Exception as e:
        logger.error(f"Erro fatal: {e}", exc_info=True)

if __name__ == "__main__":
    _run()
