# Gênesis v18.3 - Edição Sniper (WebSocket)
import os
import asyncio
import logging
import json
import uuid
import time
from typing import List, Dict, Any
from decimal import Decimal, getcontext, ROUND_DOWN
from websockets.client import connect
import aiohttp
from collections import deque

import gate_api
from gate_api.exceptions import ApiException, GateApiException

from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# --- 1. CONFIGURAÇÕES GLOBAIS ---
GATEIO_API_KEY = os.getenv("GATEIO_API_KEY")
GATEIO_SECRET_KEY = os.getenv("GATEIO_SECRET_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")

# --- Pilares da Estratégia v18.3 ---
TAXA_OPERACAO = Decimal("0.01")
MIN_PROFIT_DEFAULT = Decimal("0.05")
MARGEM_DE_SEGURANCA = Decimal("0.995")
MOEDAS_BASE_OPERACIONAL = ["USDT", "USDC"]
ORDER_BOOK_DEPTH = 100

# --- Configuração do Stop Loss ---
STOP_LOSS_LEVEL_1 = Decimal("-0.5")
STOP_LOSS_LEVEL_2 = Decimal("-1.0")

# --- NOVO: Configuração de Log para o Buffer ---
LOG_BUFFER = deque(maxlen=50)

class BufferHandler(logging.Handler):
    def emit(self, record):
        LOG_BUFFER.append(self.format(record))

# Configurar logging para enviar para o console e para o nosso buffer
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)
buffer_handler = BufferHandler()
logger.addHandler(buffer_handler)
getcontext().prec = 30

# --- Helpers ---
def _safe_decimal(value):
    try:
        if value is None or value == '':
            return Decimal("0")
        return Decimal(str(value))
    except Exception:
        return Decimal("0")

# --- 2. GATEIO API CLIENT (REST) ---
class GateIOApiClient:
    def __init__(self, api_key, secret_key):
        self.configuration = gate_api.Configuration(key=api_key, secret=secret_key)
        self.api_client = gate_api.ApiClient(self.configuration)
        self.spot_api = gate_api.SpotApi(self.api_client)

    async def _execute_api_call(self, api_call, *args, **kwargs):
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, lambda: api_call(*args, **kwargs))
        except GateApiException as ex:
            logger.error(f"GateApiException: {ex.label} - {ex.message}")
            return ex
        except ApiException as e:
            logger.error(f"ApiException: {e}")
            return None
        except Exception as e:
            logger.error(f"Unknown API error: {e}")
            return None

    async def get_all_pairs(self):
        return await self._execute_api_call(self.spot_api.list_currency_pairs)

    async def get_spot_balances(self):
        return await self._execute_api_call(self.spot_api.list_spot_accounts)

    async def create_order(self, order: gate_api.Order):
        return await self._execute_api_call(self.spot_api.create_order, order)

# --- 3. LÓGICA DE ARBITRAGEM E EXECUÇÃO ---
class GenesisEngine:
    def __init__(self, application: Application):
        self.app = application
        self.bot_data = application.bot_data
        self.api_client = GateIOApiClient(GATEIO_API_KEY, GATEIO_SECRET_KEY)
        self.bot_data.setdefault("is_running", True)
        self.bot_data.setdefault("min_profit", MIN_PROFIT_DEFAULT)
        self.bot_data.setdefault("dry_run", True)
        self.bot_data.setdefault("verbose_debug", False)
        self.pair_rules = {}
        self.rotas_monitoradas = []
        self.order_books_cache = {}
        self.trade_lock = asyncio.Lock()
        self.stats = {
            "start_time": time.time(),
            "ciclos_verificacao_total": 0,
            "rotas_sobreviventes_total": 0,
            "oportunidades_encontradas": 0,
            "trades_executados": 0
        }
    
    # --- Funções de Lógica ---
    def construir_todas_rotas_triangulares(self, all_pairs: List[str]) -> List[List[str]]:
        """
        Constrói todas as rotas de arbitragem triangular possíveis.
        """
        rotas_encontradas = []
        
        # Mapa de moedas para os pares de negociação que as contêm
        moedas_map = {}
        for pair in all_pairs:
            base, quote = pair.split('_')
            moedas_map.setdefault(base, set()).add(pair)
            moedas_map.setdefault(quote, set()).add(pair)
        
        for moeda1, pares1 in moedas_map.items():
            for par1 in pares1:
                base1, quote1 = par1.split('_')
                moeda_meio = base1 if quote1 == moeda1 else quote1
                
                # Se a moeda do meio não tiver pares, pular
                if moeda_meio not in moedas_map:
                    continue
                
                for par2 in moedas_map[moeda_meio]:
                    base2, quote2 = par2.split('_')
                    moeda3 = base2 if quote2 == moeda_meio else quote2
                    
                    # Evitar loops com a mesma moeda inicial
                    if moeda3 == moeda1:
                        continue
                    
                    for par3 in moedas_map[moeda3]:
                        base3, quote3 = par3.split('_')
                        if (base3 == moeda3 and quote3 == moeda1) or (base3 == moeda1 and quote3 == moeda3):
                            rotas_encontradas.append([par1, par2, par3])
        
        return rotas_encontradas

    async def simular_e_verificar_oportunidade(self, rota: List[str], min_profit: Decimal):
        investimento_inicial = Decimal("100")
        valor_simulado = investimento_inicial
        
        try:
            for pair_id in rota:
                if pair_id not in self.order_books_cache or not self.order_books_cache[pair_id].get('bids') or not self.order_books_cache[pair_id].get('asks'):
                    return None, None
                
                is_buy_side = rota.index(pair_id) % 2 == 0
                
                if is_buy_side:
                    best_price = Decimal(self.order_books_cache[pair_id]['asks'][0]['p'])
                    valor_simulado = (valor_simulado / best_price) * (Decimal("1") - TAXA_OPERACAO)
                else:
                    best_price = Decimal(self.order_books_cache[pair_id]['bids'][0]['p'])
                    valor_simulado = (valor_simulado * best_price) * (Decimal("1") - TAXA_OPERACAO)
                
            lucro_final = valor_simulado - investimento_inicial
            profit_percent = (lucro_final / investimento_inicial) * 100 if investimento_inicial > 0 else Decimal("0")

            if profit_percent > min_profit:
                return rota, profit_percent
            return None, None

        except Exception as e:
            logger.error(f"Erro na simulação para a rota {rota}: {e}")
            return None, None

    def _get_pair_details(self, coin_from, coin_to):
        pair_v1 = f"{coin_from}_{coin_to}"
        if pair_v1 in self.pair_rules: return pair_v1, "sell"
        pair_v2 = f"{coin_to}_{coin_from}"
        if pair_v2 in self.pair_rules: return pair_v2, "buy"
        return None, None
    
    # --- Funções de Segurança e Execução (STOP LOSS / SAÍDA DE EMERGÊNCIA) ---
    async def _reverter_para_moeda_base(self, current_currency, current_amount):
        if current_currency in MOEDAS_BASE_OPERACIONAL:
            return
        for base_currency in MOEDAS_BASE_OPERACIONAL:
            pair_id, side = self._get_pair_details(current_currency, base_currency)
            if pair_id:
                await send_telegram_message(f"🚨 **SAÍDA DE EMERGÊNCIA ATIVADA!**\nTentando converter `{current_currency}` para `{base_currency}`.")
                await self._fechar_posicao(current_amount, pair_id, side)
                return
        await send_telegram_message(f"❌ **FALHA CRÍTICA NA SAÍDA DE EMERGÊNCIA!**\nNão foi possível encontrar um par para converter `{current_currency}`.")

    async def _fechar_posicao(self, amount_to_close, pair_id, side):
        pair_info = self.pair_rules.get(pair_id)
        if not pair_info: return
        quantizer = Decimal(f"1e-{pair_info['amount_precision']}")
        amount_quantized = amount_to_close.quantize(quantizer, rounding=ROUND_DOWN)
        if pair_info.get("min_base_amount") and amount_quantized < pair_info["min_base_amount"]:
            return
        try:
            order_params = gate_api.Order(currency_pair=pair_id, type="market", account="spot", side=side, amount=str(amount_quantized))
            res = await self.api_client.create_order(order_params)
            if not isinstance(res, GateApiException):
                logger.info(f"Posicao de {pair_id} fechada com sucesso.")
            else:
                logger.error(f"Falha ao fechar posicao para {pair_id}: {res.message}.")
        except Exception as e:
            logger.error(f"Erro inesperado ao tentar fechar posição: {e}", exc_info=True)
            
    async def _executar_trade_realista(self, cycle_path):
        is_dry_run = self.bot_data.get("dry_run", True)
        moeda_inicial_rota = cycle_path[0].split('_')[0]
        final_currency = moeda_inicial_rota

        try:
            saldos_pre_trade = await self.api_client.get_spot_balances()
            investimento_inicial = sum(Decimal(c.available) for c in saldos_pre_trade if c.currency == moeda_inicial_rota and c.available)

            profit_rota = await self.simular_e_verificar_oportunidade(cycle_path, Decimal("-100")) # -100% para sempre retornar o valor

            if is_dry_run:
                await send_telegram_message(f"🎯 **Alvo Realista na Mira (Simulação)**\n"
                                            f"Rota: `{' -> '.join(cycle_path)}`\n"
                                            f"Investimento: `{investimento_inicial:.4f} {moeda_inicial_rota}`\n"
                                            f"Lucro Líquido Realista: `{(profit_rota[1] if profit_rota is not None else Decimal('0')):.4f}%`")
                return

            await send_telegram_message(f"🚀 **Iniciando Trade REAL...**\n"
                                        f"Rota: `{' -> '.join(cycle_path)}`\n"
                                        f"Investimento Planejado: `{investimento_inicial:.4f} {moeda_inicial_rota}`")
            
            current_amount = investimento_inicial
            for i in range(len(cycle_path)):
                pair_id = cycle_path[i]
                
                # Definir moedas de origem e destino para o trade atual
                base_coin, quote_coin = pair_id.split('_')
                if i == 0:
                    coin_from = moeda_inicial_rota
                    coin_to = base_coin if base_coin != moeda_inicial_rota else quote_coin
                elif i < len(cycle_path) - 1:
                    coin_from = final_currency # Moeda do trade anterior
                    coin_to = base_coin if base_coin != coin_from else quote_coin
                else:
                    coin_from = final_currency
                    coin_to = moeda_inicial_rota

                side = "buy" if pair_id == f"{coin_to}_{coin_from}" else "sell" if pair_id == f"{coin_from}_{coin_to}" else None
                if not side:
                    await send_telegram_message(f"❌ **FALHA CRÍTICA - ROTA INVÁLIDA!**\nNão foi possível determinar o lado do trade para o par `{pair_id}`. Abortando.")
                    await self._reverter_para_moeda_base(final_currency, current_amount)
                    return

                if current_amount <= 0:
                    await send_telegram_message(f"❌ **FALHA CRÍTICA (Passo {i+1})**\nSaldo de `{coin_from}` é zero. Abortando.")
                    await self._reverter_para_moeda_base(final_currency, current_amount)
                    return

                pair_info = self.pair_rules.get(pair_id)
                amount_quantizer = Decimal(f"1e-{pair_info['amount_precision']}")
                amount_to_send = (current_amount * MARGEM_DE_SEGURANCA).quantize(amount_quantizer, rounding=ROUND_DOWN)
                
                if amount_to_send <= 0:
                    await send_telegram_message(f"❌ **FALHA CRÍTICA (Passo {i+1})**\nSaldo de `{coin_from}` (`{current_amount}`) é muito pequeno. Abortando.")
                    await self._reverter_para_moeda_base(final_currency, current_amount)
                    return

                order_params = gate_api.Order(currency_pair=pair_id, type="market", account="spot", side=side, time_in_force="ioc", text=f"t-gnsis-{uuid.uuid4().hex[:10]}", amount=str(amount_to_send))
                await send_telegram_message(f"⏳ Passo {i+1}/{len(cycle_path)}: Negociando `{amount_to_send} {coin_from}` para `{coin_to}` no par `{pair_id}`.")
                order_result = await self.api_client.create_order(order_params)
                
                if isinstance(order_result, GateApiException):
                    await send_telegram_message(f"❌ **FALHA NO PASSO {i+1} ({pair_id})**\n**Motivo:** `{order_result.message}`\n**ALERTA:** Saldo em `{coin_from}` pode estar preso!")
                    await self._reverter_para_moeda_base(final_currency, current_amount)
                    return

                await asyncio.sleep(2)

                saldos_step_depois = await self.api_client.get_spot_balances()
                current_amount = sum(Decimal(c.available) for c in saldos_step_depois if c.currency == coin_to and c.available)
                
                if current_amount <= 0:
                     await send_telegram_message(f"❌ **FALHA CRÍTICA - TRADE NÃO EXECUTADO!**\nO saldo em `{coin_to}` não foi atualizado. Abortando.")
                     await self._reverter_para_moeda_base(coin_from, amount_to_send)
                     return

                final_currency = coin_to
                logger.info(f"Passo {i+1} concluido. Saldo atual: {current_amount} {final_currency}.")

                # --- Lógica de Stop Loss Dinâmico ---
                if i < len(cycle_path) - 1: # Não aplicar no último passo
                    try:
                        # Obter o preço atual do par intermediário
                        current_price_data = self.order_books_cache.get(f"{final_currency}_{moeda_inicial_rota}") or self.order_books_cache.get(f"{moeda_inicial_rota}_{final_currency}")
                        if not current_price_data:
                            logger.warning("Não foi possível obter preço para stop loss. Continuando...")
                            continue

                        current_price = Decimal(current_price_data['bids'][0]['p'] if f"{final_currency}_{moeda_inicial_rota}" in self.order_books_cache else current_price_data['asks'][0]['p'])
                        current_value = current_amount * current_price

                        perda_percentual = ((current_value - investimento_inicial) / investimento_inicial) * 100
                        
                        if perda_percentual <= STOP_LOSS_LEVEL_1:
                            await send_telegram_message(f"⚠️ **STOP LOSS ATINGIDO!**\nPerda de `{perda_percentual:.4f}%` detectada. Iniciando reversão.")
                            await self._reverter_para_moeda_base(final_currency, current_amount)
                            return

                    except Exception as e:
                        logger.error(f"Erro na verificação de stop loss: {e}", exc_info=True)


            saldos_finais = await self.api_client.get_spot_balances()
            saldo_final_trade = sum(Decimal(c.available) for c in saldos_finais if c.currency == cycle_path[-1].split('_')[-1] and c.available)
            lucro_final = saldo_final_trade - investimento_inicial
            lucro_percent = (lucro_final / investimento_inicial) * 100 if investimento_inicial > 0 else 0
            
            await send_telegram_message(f"✅ **Trade Concluído (Gate.io)!**\n"
                                        f"Rota: `{' -> '.join(cycle_path)}`\n"
                                        f"Investimento: `{investimento_inicial:.4f} {moeda_inicial_rota}`\n"
                                        f"Resultado: `{saldo_final_trade:.4f} {cycle_path[-1].split('_')[-1]}`\n"
                                        f"Lucro/Prejuízo: `{lucro_final:.4f} {moeda_inicial_rota}` (`{lucro_percent:.4f}%`)")
            self.stats["trades_executados"] += 1
        except Exception as e:
            logger.error(f"Erro durante a execução do trade realista: {e}", exc_info=True)
            await send_telegram_message(f"❌ Erro crítico durante o trade: `{e}`")
            saldos_atuais_erro = await self.api_client.get_spot_balances()
            saldo_na_moeda = sum(Decimal(c.available) for c in saldos_atuais_erro if c.currency == final_currency and c.available)
            if saldo_na_moeda > 0:
                await self._reverter_para_moeda_base(final_currency, saldo_na_moeda)
        finally:
            if self.trade_lock.locked(): self.trade_lock.release()
            logger.info(f"Trade para rota {' -> '.join(cycle_path)} finalizado. Trade lock liberado.")

    # --- Funções de Inicialização e Loop Principal (WebSocket) ---
    async def get_all_pairs_from_api_v2(self):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get("https://api.gateio.ws/api/v4/spot/currency_pairs") as response:
                    response.raise_for_status()
                    pairs_data = await response.json()
                    
                    for pair_data in pairs_data:
                        if getattr(pair_data, "trade_status", None) == "tradable":
                            base, quote = pair_data['base'], pair_data['quote']
                            self.pair_rules[pair_data['id']] = {
                                "base": base, "quote": quote,
                                "amount_precision": int(pair_data.get("amount_precision", 8)),
                                "price_precision": int(pair_data.get("precision", 8)),
                                "min_base_amount": _safe_decimal(pair_data.get("min_base_amount", None)),
                                "min_quote_amount": _safe_decimal(pair_data.get("min_quote_amount", None)),
                            }
                    return [pair['id'] for pair in pairs_data]
        except Exception as e:
            print(f"Erro ao obter pares da API: {e}")
            return []

    async def inicializar(self):
        logger.info("Gênesis v18.3 (Edição Sniper): Iniciando...")
        await send_telegram_message("⏳ **Gênesis v18.3 (Sniper):** Iniciando motor... Buscando dados da Gate.io.")
        
        all_pairs_list = await self.get_all_pairs_from_api_v2()
        if not all_pairs_list:
            logger.critical("Gênesis: Não foi possível obter os pares da Gate.io.")
            await send_telegram_message("❌ **Gênesis ERRO:** Falha ao conectar à Gate.io.")
            return

        self.rotas_monitoradas = self.construir_todas_rotas_triangulares(all_pairs_list)
        
        num_rotas = len(self.rotas_monitoradas)
        self.bot_data["total_ciclos"] = num_rotas
        
        logger.info(f"Gênesis: Processo de busca COMPLETA concluído. Total de {num_rotas} rotas triangulares encontradas.")
        await send_telegram_message(f"✅ **Gênesis SNIPER Ativado:** Encontradas **{num_rotas}** rotas de alto potencial para monitoramento.")

        pares_necessarios = set()
        for rota in self.rotas_monitoradas:
            for pair_id in rota:
                pares_necessarios.add(pair_id)

        # Inicia a tarefa de monitoramento WebSocket
        asyncio.create_task(self.monitorar_websocket(list(pares_necessarios)))
        
    async def monitorar_websocket(self, pares_necessarios: List[str]):
        uri = "wss://ws.gate.io/v4/"
        # Lotes de 35 pares
        batch_size = 35 
        
        while True:
            try:
                async with connect(uri) as websocket:
                    logger.info("Conectado ao WebSocket. Assinando canais de livro de ordens...")
                    
                    # Assinar pares em lotes de 35
                    for i in range(0, len(pares_necessarios), batch_size):
                        batch = pares_necessarios[i:i + batch_size]
                        for pair in batch:
                            subscribe_message = {
                                "time": int(time.time()),
                                "channel": "spot.order_book",
                                "event": "subscribe",
                                "payload": [pair, "20", "100ms"]
                            }
                            await websocket.send(json.dumps(subscribe_message))
                            logger.info(f"Assinatura enviada para {pair}")
                            await asyncio.sleep(0.05)
                        await send_telegram_message(f"✅ Lote de {len(batch)} pares assinado com sucesso.")
                        await asyncio.sleep(1) # Intervalo entre os lotes
                    
                    logger.info("Todas as assinaturas de pares foram enviadas.")

                    while True:
                        if not self.bot_data.get("is_running", True) or self.trade_lock.locked():
                            await asyncio.sleep(1)
                            continue
                            
                        try:
                            # Tentar receber dados com um timeout curto
                            message = await asyncio.wait_for(websocket.recv(), timeout=25)
                            data = json.loads(message)
                        except asyncio.TimeoutError:
                            # Se não houver dados, enviar um ping do cliente
                            ping_message = {
                                "time": int(time.time()),
                                "channel": "spot.ping"
                            }
                            await websocket.send(json.dumps(ping_message))
                            logger.info("Ping do cliente enviado.")
                            continue
                        except Exception as e:
                            logger.error(f"Erro ao receber ou processar dados do WebSocket: {e}", exc_info=True)
                            break # Quebra o loop interno para tentar reconectar
                        
                        logger.info(f"Dados brutos do WebSocket recebidos: {message[:150]}...")

                        if data.get('channel') == 'spot.ping':
                            pong_message = {
                                "time": int(time.time()),
                                "channel": "spot.pong"
                            }
                            await websocket.send(json.dumps(pong_message))
                            logger.info("Pong enviado em resposta ao ping do servidor. Conexão ativa.")
                            continue

                        if data.get('channel') == 'spot.pong':
                            logger.info("Pong recebido. Conexão ativa.")
                            continue

                        # Verificação do canal para 'spot.order_book'
                        if data.get('channel') == 'spot.order_book' and data.get('event') == 'update':
                            self.stats["ciclos_verificacao_total"] += 1
                            pair = data['result']['s']
                            
                            self.order_books_cache[pair] = {
                                'bids': [{'p': p, 's': s} for p, s in data['result']['b']],
                                'asks': [{'p': p, 's': s} for p, s in data['result']['a']]
                            }

                            for rota in self.rotas_monitoradas:
                                if pair in rota:
                                    rota_encontrada, lucro = await self.simular_e_verificar_oportunidade(rota, self.bot_data["min_profit"])
                                    if rota_encontrada:
                                        self.stats["oportunidades_encontradas"] += 1
                                        async with self.trade_lock:
                                            await self._executar_trade_realista(rota_encontrada)
                            
                        await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"Erro na conexão WebSocket: {e}. Tentando reconectar em 5s...", exc_info=True)
                await send_telegram_message(f"❌ **ERRO WEBSOCKET:** `{e}`. Tentando reconectar...")
                await asyncio.sleep(5)

# --- 4. TELEGRAM INTERFACE E COMANDOS ---
async def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not ADMIN_CHAT_ID: return
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Erro ao enviar mensagem no Telegram: {e}")

# Função para enviar mensagens longas, fragmentando-as
async def send_long_message(context: ContextTypes.DEFAULT_TYPE, title: str, text: str):
    MAX_LENGTH = 4096
    full_message = f"**{title}**\n\n```\n{text}\n```"

    if len(full_message) <= MAX_LENGTH:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=full_message, parse_mode='Markdown')
        return

    # Se for muito longo, divide em partes
    messages = []
    current_chunk = f"**{title}**\n\n```\n"
    for line in text.split('\n'):
        if len(current_chunk) + len(line) + 1 + 3 > MAX_LENGTH: # +1 para \n, +3 para ```
            messages.append(current_chunk + "```")
            current_chunk = "```\n" + line + "\n"
        else:
            current_chunk += line + "\n"
    
    if len(current_chunk) > 3: # Adiciona o último pedaço
        messages.append(current_chunk + "```")
    
    for msg in messages:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=msg, parse_mode='Markdown')
        await asyncio.sleep(0.5) # Evita flood da API

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Olá! Gênesis v18.3 (Gate.io) online. Use /status para começar.")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bd = context.bot_data
    engine: GenesisEngine = bd.get('engine')
    if not engine:
        await update.message.reply_text("O motor ainda não foi inicializado.")
        return
        
    status_text = "▶️ Rodando" if bd.get('is_running') else "⏸️ Pausado"
    if bd.get('is_running') and engine.trade_lock.locked():
        status_text = "▶️ Rodando (Processando Alvo)"
        
    msg = (f"**📊 Painel de Controle - Gênesis v18.3 (Sniper)**\n\n"
           f"**Estado:** `{status_text}`\n"
           f"**Modo:** `{'Simulação' if bd.get('dry_run') else '🔴 REAL'}`\n"
           f"**Estratégia:** `Arbitragem Triangular`\n"
           f"**Lucro Mínimo (Líquido):** `{bd.get('min_profit')}%`\n"
           f"**Total de Rotas Monitoradas:** `{len(engine.rotas_monitoradas)}`")
    await update.message.reply_text(msg, parse_mode='Markdown')

async def radar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Função não aplicável com a estratégia WebSocket de alta velocidade. O radar agora é o console.")

async def debug_radar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Função descontinuada. O log agora é o radar.")

async def diagnostico_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("O motor ainda não foi inicializado.")
        return
    
    uptime_seconds = time.time() - engine.stats['start_time']
    m, s = divmod(uptime_seconds, 60)
    h, m = divmod(m, 60)
    uptime_str = f"{int(h)}h {int(m)}m {int(s)}s"
    
    msg = (f"**🩺 Diagnóstico Interno - Gênesis v18.3**\n\n"
           f"**Ativo há:** `{uptime_str}`\n"
           f"**Motor Principal:** `{'ATIVO' if context.bot_data.get('is_running') else 'PAUSADO'}`\n"
           f"**Trava de Trade:** `{'BLOQUEADO (em trade)' if engine.trade_lock.locked() else 'LIVRE'}`\n"
           f"**Conexão WebSocket:** `ATIVA`\n"
           f"**Taxa de Atualização:** `~100ms`\n\n"
           f"--- **Estatísticas Totais da Sessão** ---\n"
           f"**Atualizações de Preço:** `{engine.stats['ciclos_verificacao_total']}`\n"
           f"**Oportunidades Encontradas:** `{engine.stats['oportunidades_encontradas']}`\n"
           f"**Trades Executados:** `{engine.stats['trades_executados']}`\n")
    await update.message.reply_text(msg, parse_mode='Markdown')

# --- NOVO: Comando de Log ---
async def log_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not LOG_BUFFER:
        await update.message.reply_text("Nenhum log disponível.")
        return
    
    log_content = "\n".join(list(LOG_BUFFER))
    await send_long_message(context, "📝 Logs Recentes do Bot", log_content)


async def saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("A conexão com a exchange ainda não foi estabelecida.")
        return
    await update.message.reply_text("Buscando saldos na Gate.io...")
    try:
        saldos = await engine.api_client.get_spot_balances()
        if not saldos or isinstance(saldos, GateApiException):
            await update.message.reply_text(f"❌ Erro ao buscar saldos: {saldos.message if isinstance(saldos, GateApiException) else 'Resposta vazia'}")
            return
        msg = "**💰 Saldos Atuais (Spot Gate.io)**\n\n"
        non_zero_saldos = [c for c in saldos if Decimal(c.available) > 0]
        if not non_zero_saldos:
            await update.message.reply_text("Nenhum saldo encontrado.")
            return
        for conta in non_zero_saldos:
            msg += f"**{conta.currency}:** `{Decimal(conta.available)}`\n"
        await update.message.reply_text(msg, parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Erro ao buscar saldos: `{e}`")

async def modo_real_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['dry_run'] = False
    await update.message.reply_text("🔴 **MODO REAL ATIVADO (Gate.io).**")
    await status_command(update, context)

async def modo_simulacao_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['dry_run'] = True
    await update.message.reply_text("🔵 **Modo Simulação Ativado (Gate.io).**")
    await status_command(update, context)

async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_profit = Decimal(context.args[0])
        context.bot_data['min_profit'] = new_profit
        await update.message.reply_text(f"✅ Lucro mínimo (Gate.io) definido para **{new_profit}%**.")
    except (IndexError, TypeError, ValueError):
        await update.message.reply_text("⚠️ Uso: `/setlucro 0.05`")

async def pausar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['is_running'] = False
    await update.message.reply_text("⏸️ **Bot (Gate.io) pausado.**")
    await status_command(update, context)

async def retomar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.bot_data['is_running'] = True
    await update.message.reply_text("✅ **Bot (Gate.io) retomado.**")
    await status_command(update, context)

async def salvar_saldo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    engine: GenesisEngine = context.bot_data.get('engine')
    if not engine:
        await update.message.reply_text("O motor ainda não foi inicializado.")
        return
    try:
        moeda = context.args[0].upper()
        if moeda not in MOEDAS_BASE_OPERACIONAL:
            await update.message.reply_text(f"⚠️ A moeda `{moeda}` não é uma das moedas base configuradas.")
            return
        
        await update.message.reply_text(f"Buscando informações para `{moeda}`...")

        base_alvo = MOEDAS_BASE_OPERACIONAL[0]
        pair_id, side = engine._get_pair_details(moeda, base_alvo)

        if not pair_id:
            await update.message.reply_text(f"Não foi possível encontrar um par para `{moeda}` com `{base_alvo}`.")
            return
        
        saldos = await engine.api_client.get_spot_balances()
        saldo_moeda = sum(Decimal(c.available) for c in saldos if c.currency == moeda and c.available)

        if saldo_moeda > 0:
            await engine._fechar_posicao(saldo_moeda, pair_id, side)
            await update.message.reply_text(f"✅ Ordem de conversão de `{moeda}` para `{base_alvo}` enviada.")
        else:
            await update.message.reply_text(f"⚠️ Saldo de `{moeda}` é zero.")
    except (IndexError, TypeError, ValueError):
        await update.message.reply_text(f"⚠️ Uso: `/salvar_saldo USDC` (Tenta vender o saldo de USDC para {MOEDAS_BASE_OPERACIONAL[0]}).")
    except Exception as e:
        logger.error(f"Erro inesperado no /salvar_saldo: {e}")
        await update.message.reply_text(f"❌ Erro inesperado ao tentar salvar saldo: {e}")
        
async def post_init_tasks(app: Application):
    logger.info("Iniciando motor Gênesis v18.3 (Gate.io)...")
    if not all([GATEIO_API_KEY, GATEIO_SECRET_KEY, TELEGRAM_TOKEN, ADMIN_CHAT_ID]):
        logger.critical("❌ Falha crítica: Variáveis de ambiente incompletas.")
        return
    engine = GenesisEngine(app)
    app.bot_data['engine'] = engine
    try:
        await engine.inicializar()
        logger.info("Motor e tarefas de fundo iniciadas.")
    except Exception as e:
        logger.error(f"❌ Erro ao inicializar o motor: {e}", exc_info=True)
        await send_telegram_message("❌ ERRO CRÍTICO: Não foi possível inicializar o bot. Verifique os logs.")
        
def main():
    if not TELEGRAM_TOKEN:
        logger.critical("Token do Telegram não encontrado.")
        return
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    command_map = {
        "start": start_command,
        "status": status_command,
        "radar": radar_command,
        "debug_radar": debug_radar_command,
        "diagnostico": diagnostico_command,
        "saldo": saldo_command,
        "modo_real": modo_real_command,
        "modo_simulacao": modo_simulacao_command,
        "setlucro": setlucro_command,
        "pausar": pausar_command,
        "retomar": retomar_command,
        "salvar_saldo": salvar_saldo_command,
        "log": log_command # NOVO COMANDO AQUI
    }
    for command, handler in command_map.items():
        application.add_handler(CommandHandler(command, handler))

    application.post_init = post_init_tasks
    logger.info("Iniciando bot do Telegram...")
    application.run_polling()

if __name__ == "__main__":
    main()
