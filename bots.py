# -*- coding: utf-8 -*-
import os
import sys
import time
import hmac
import base64
import requests
import json
import threading
import sqlite3
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, getcontext, ROUND_DOWN
from dotenv import load_dotenv
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import signal

# ==============================================================================
# 1. CONFIGURAÇÃO GLOBAL E INICIALIZAÇÃO
# ==============================================================================
load_dotenv()
getcontext().prec = 28
getcontext().rounding = ROUND_DOWN

# --- Chaves e Tokens ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_API_SECRET = os.getenv("OKX_API_SECRET", "")
OKX_API_PASSPHRASE = os.getenv("OKX_API_PASSPHRASE", "")

API_KEYS_FUTURES = {
    'okx': {'apiKey': OKX_API_KEY, 'secret': OKX_API_SECRET, 'password': OKX_API_PASSPHRASE},
    'gateio': {'apiKey': os.getenv('GATEIO_API_KEY'), 'secret': os.getenv('GATEIO_API_SECRET')},
    'mexc': {'apiKey': os.getenv('MEXC_API_KEY'), 'secret': os.getenv('MEXC_API_SECRET')},
    'bitget': {'apiKey': os.getenv('BITGET_API_KEY'), 'secret': os.getenv('BITGET_API_SECRET'), 'password': os.getenv('BITGET_API_PASSPHRASE')},
}

# --- Importações Condicionais ---
try:
    import ccxt.async_support as ccxt
except ImportError:
    ccxt = None

# --- Variáveis de estado globais ---
triangular_running = True
futures_running = True
triangular_min_profit_threshold = Decimal(os.getenv("MIN_PROFIT_THRESHOLD", "0.002"))
futures_min_profit_threshold = Decimal(os.getenv("FUTURES_MIN_PROFIT_THRESHOLD", "0.3"))
triangular_simulate = False
futures_dry_run = os.getenv("FUTURES_DRY_RUN", "true").lower() in ["1", "true", "yes"]
futures_trade_limit = int(os.getenv("FUTURES_TRADE_LIMIT", "0"))
futures_trades_executed = 0

# --- Configurações de Volume de Trade ---
triangular_trade_amount = Decimal("1")
triangular_trade_amount_is_percentage = False
futures_trade_amount = Decimal(os.getenv("FUTURES_TRADE_AMOUNT_USDT", "10"))
futures_trade_amount_is_percentage = False

# --- Monitoramento de Erros de Conexão ---
connection_errors = {} # Dicionário para rastrear erros por exchange

# ==============================================================================
# 2. FUNÇÕES AUXILIARES GLOBAIS
# ==============================================================================
async def send_telegram_message(text, chat_id=None, update: Update = None):
    final_chat_id = chat_id or (update.effective_chat.id if update else TELEGRAM_CHAT_ID)
    if not TELEGRAM_TOKEN or not final_chat_id: return
    bot = Bot(token=TELEGRAM_TOKEN)
    try:
        await bot.send_message(chat_id=final_chat_id, text=text, parse_mode="Markdown")
    except Exception as e:
        print(f"Erro ao enviar mensagem no Telegram: {e}")

async def get_okx_usdt_balance():
    """Função específica para obter o saldo USDT da OKX via API REST."""
    try:
        url = "https://www.okx.com/api/v5/account/balance?ccy=USDT"
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        method = 'GET'
        request_path = '/api/v5/account/balance?ccy=USDT'
        message = timestamp + method + request_path
        mac = hmac.new(bytes(OKX_API_SECRET, encoding='utf8'), bytes(message, encoding='utf-8'), digestmod='sha256')
        sign = base64.b64encode(mac.digest())
        
        headers = {
            'OK-ACCESS-KEY': OKX_API_KEY,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': timestamp,
            'OK-ACCESS-PASSPHRASE': OKX_API_PASSPHRASE,
            'Content-Type': 'application/json'
        }
        
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data['code'] == '0' and data['data']:
            details = data['data'][0]['details']
            for detail in details:
                if detail['ccy'] == 'USDT':
                    return Decimal(detail.get('availBal', '0'))
        return Decimal('0')
    except Exception as e:
        print(f"Erro ao buscar saldo OKX: {e}")
        return None

async def get_trade_amount(exchange_name, symbol, is_triangular):
    amount_value = triangular_trade_amount if is_triangular else futures_trade_amount
    is_percentage = triangular_trade_amount_is_percentage if is_triangular else futures_trade_amount_is_percentage

    if not is_percentage:
        return amount_value

    try:
        available_usdt = Decimal('0')
        if is_triangular:
            balance = await get_okx_usdt_balance()
            if balance is not None:
                available_usdt = balance
        elif ccxt and exchange_name in active_futures_exchanges:
            ex = active_futures_exchanges[exchange_name]
            balance_data = await ex.fetch_balance()
            available_usdt = Decimal(balance_data.get('free', {}).get('USDT', 0))

        if available_usdt <= 0:
            raise ValueError("Saldo USDT disponível é zero ou não pôde ser obtido.")

        calculated_amount = available_usdt * (amount_value / 100)
        return calculated_amount

    except Exception as e:
        await send_telegram_message(f"⚠️ *Erro ao calcular volume:* `{e}`. Usando valor padrão: `{amount_value}` USDT.")
        return amount_value if not is_percentage else Decimal('1')

# ==============================================================================
# 3. MÓDULO DE ARBITRAGEM TRIANGULAR (OKX SPOT)
# ==============================================================================
TRIANGULAR_DB_FILE = "/tmp/historico_triangular.db"
TRIANGULAR_FEE_RATE = Decimal("0.001")
triangular_monitored_cycles_count = 0
triangular_lucro_total_usdt = Decimal("0")

def init_triangular_db():
    with sqlite3.connect(TRIANGULAR_DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS ciclos (
            timestamp TEXT, pares TEXT, lucro_percent REAL, lucro_usdt REAL, modo TEXT, status TEXT, detalhes TEXT)""")
        conn.commit()

def registrar_ciclo_triangular(pares, lucro_percent, lucro_usdt, modo, status, detalhes=""):
    global triangular_lucro_total_usdt
    triangular_lucro_total_usdt += Decimal(str(lucro_usdt))
    with sqlite3.connect(TRIANGULAR_DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO ciclos VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (datetime.now(timezone.utc).isoformat(), json.dumps(pares), float(lucro_percent),
                   float(lucro_usdt), modo, status, detalhes))
        conn.commit()

def get_all_okx_spot_instruments():
    url = "https://www.okx.com/api/v5/public/instruments?instType=SPOT"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.json().get("data", [])

def build_dynamic_cycles(instruments):
    main_currencies = {'BTC', 'ETH', 'USDC', 'OKB'}
    pairs_by_quote = {}
    for inst in instruments:
        quote_ccy = inst.get('quoteCcy')
        if quote_ccy not in pairs_by_quote:
            pairs_by_quote[quote_ccy] = []
        pairs_by_quote[quote_ccy].append(inst)
    cycles = []
    if 'USDT' in pairs_by_quote:
        for pair1 in pairs_by_quote['USDT']:
            base1 = pair1['baseCcy']
            for pivot in main_currencies:
                if base1 == pivot: continue
                for pair2 in pairs_by_quote.get(pivot, []):
                    if pair2['baseCcy'] == base1:
                        cycle = [
                            (f"{base1}-USDT", "buy"),
                            (f"{base1}-{pivot}", "sell"),
                            (f"{pivot}-USDT", "sell")
                        ]
                        cycles.append(cycle)
    return cycles

def get_okx_spot_tickers(inst_ids):
    tickers = {}
    chunks = [inst_ids[i:i + 100] for i in range(0, len(inst_ids), 100)]
    for chunk in chunks:
        url = f"https://www.okx.com/api/v5/market/tickers?instType=SPOT&instId={','.join(chunk)}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json().get("data", [])
        for d in data:
            if d.get("bidPx") and d.get("askPx"):
                tickers[d["instId"]] = {"bid": Decimal(d["bidPx"]), "ask": Decimal(d["askPx"])}
    return tickers

async def simulate_triangular_cycle(cycle, tickers):
    amt = await get_trade_amount('okx', 'N/A', is_triangular=True)
    if amt <= 0:
        return Decimal("0"), Decimal("0")
    start_amt = amt
    current_amt = amt
    for instId, action in cycle:
        ticker = tickers.get(instId)
        if not ticker: raise RuntimeError(f"Ticker para {instId} não encontrado.")
        price = ticker["ask"] if action == "buy" else ticker["bid"]
        fee = current_amt * TRIANGULAR_FEE_RATE
        if action == "buy":
            current_amt = (current_amt - fee) / price
        elif action == "sell":
            current_amt = (current_amt * price) - fee
    final_usdt = current_amt
    profit_abs = final_usdt - start_amt
    profit_pct = profit_abs / start_amt if start_amt > 0 else 0
    return profit_pct, profit_abs

async def loop_bot_triangular():
    global triangular_monitored_cycles_count
    print("[INFO] Bot de Arbitragem Triangular (OKX Spot) iniciado.")
    try:
        all_instruments = get_all_okx_spot_instruments()
        dynamic_cycles = build_dynamic_cycles(all_instruments)
        triangular_monitored_cycles_count = len(dynamic_cycles)
        print(f"[INFO-TRIANGULAR] {triangular_monitored_cycles_count} ciclos construídos.")
    except Exception as e:
        await send_telegram_message(f"❌ *Erro Crítico Triangular:* Falha ao construir ciclos. Erro: `{e}`")
        return

    while True:
        if not triangular_running:
            await asyncio.sleep(30)
            continue
        try:
            all_inst_ids_needed = list({instId for cycle in dynamic_cycles for instId, _ in cycle})
            all_tickers = get_okx_spot_tickers(all_inst_ids_needed)
            for cycle in dynamic_cycles:
                try:
                    profit_est_pct, profit_est_abs = await simulate_triangular_cycle(cycle, all_tickers)
                    if profit_est_pct > triangular_min_profit_threshold:
                        pares_fmt = " → ".join([p for p, a in cycle])
                        
                        saldo_atual_usdt = await get_okx_usdt_balance()
                        saldo_texto = f"`{saldo_atual_usdt:.2f} USDT`" if saldo_atual_usdt is not None else "`Não foi possível obter`"

                        if triangular_simulate:
                            msg = (f"🚀 *Oportunidade Triangular (Simulada)*\n\n"
                                   f"`{pares_fmt}`\n"
                                   f"Lucro Previsto: `{profit_est_pct:.3%}` (~`{profit_est_abs:.4f} USDT`)\n"
                                   f"Saldo OKX: {saldo_texto}")
                            registrar_ciclo_triangular(pares_fmt, float(profit_est_pct), float(profit_est_abs), "SIMULATE", "OK")
                            await send_telegram_message(msg)
                        else:
                            msg = (f"✅ *Arbitragem Triangular (Finalizada)*\n\n"
                                   f"`{pares_fmt}`\n"
                                   f"Lucro Real: `{profit_est_pct:.3%}` (~`{profit_est_abs:.4f} USDT`)\n"
                                   f"Saldo OKX: {saldo_texto}")
                            registrar_ciclo_triangular(pares_fmt, float(profit_est_pct), float(profit_est_abs), "LIVE", "OK")
                            await send_telegram_message(msg)
                except Exception:
                    pass
        except Exception as e_loop:
            print(f"[ERRO-LOOP-TRIANGULAR] {e_loop}")
            await send_telegram_message(f"⚠️ *Erro no Bot Triangular:* `{e_loop}`")
   # ==============================================================================
# 4. MÓDULO DE ARBITRAGEM DE FUTUROS (MULTI-EXCHANGE)
# ==============================================================================
active_futures_exchanges = {}
futures_monitored_pairs_count = 0
FUTURES_TARGET_PAIRS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'BNB/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT', 
    'ADA/USDT:USDT', 'DOGE/USDT:USDT', 'AVAX/USDT:USDT', 'DOT/USDT:USDT', 'TRX/USDT:USDT',
    'MATIC/USDT:USDT', 'LINK/USDT:USDT', 'TON/USDT:USDT', 'SHIB/USDT:USDT', 'ICP/USDT:USDT',
    'LTC/USDT:USDT', 'NEAR/USDT:USDT', 'UNI/USDT:USDT', 'XLM/USDT:USDT', 'ATOM/USDT:USDT',
    'FIL/USDT:USDT', 'RUNE/USDT:USDT', 'APT/USDT:USDT', 'ARB/USDT:USDT', 'OP/USDT:USDT',
    'SUI/USDT:USDT', 'MNT/USDT:USDT', 'IMX/USDT:USDT', 'AAVE/USDT:USDT', 'GRT/USDT:USDT',
    'VET/USDT:USDT', 'ALGO/USDT:USDT', 'PEPE/USDT:USDT', 'WLD/USDT:USDT', 'AR/USDT:USDT',
    'ORDI/USDT:USDT', 'MEME/USDT:USDT', 'BONK/USDT:USDT', 'FLOKI/USDT:USDT', '1000SATS/USDT:USDT',
    'MKR/USDT:USDT', 'CRV/USDT:USDT', 'COMP/USDT:USDT', 'SNX/USDT:USDT', '1INCH/USDT:USDT',
    'ZRX/USDT:USDT', 'DYDX/USDT:USDT', 'RNDR/USDT:USDT', 'FTM/USDT:USDT', 'KAS/USDT:USDT',
    'INJ/USDT:USDT', 'TIA/USDT:USDT'
]

async def initialize_futures_exchanges():
    global active_futures_exchanges, connection_errors
    if not ccxt: return
    print("[INFO] Inicializando exchanges para FUTUROS...")
    for name, creds in API_KEYS_FUTURES.items():
        if not creds or not creds.get('apiKey'): continue
        instance = None
        try:
            exchange_class = getattr(ccxt, name)
            instance = exchange_class({**creds, 'options': {'defaultType': 'swap'}})
            await instance.load_markets()
            active_futures_exchanges[name] = instance
            print(f"[INFO-FUTUROS] Exchange '{name}' carregada.")
            if name in connection_errors:
                await send_telegram_message(f"✅ *Conexão Restaurada:* A conexão com `{name}` foi restabelecida.")
                del connection_errors[name]
        except Exception as e:
            error_msg = f"{e}"
            if name not in connection_errors or connection_errors[name] != error_msg:
                await send_telegram_message(f"❌ *Erro de Conexão:* Falha ao conectar em `{name}`: `{error_msg}`")
                connection_errors[name] = error_msg
            if instance: await instance.close()

async def find_futures_opportunities():
    global connection_errors
    tasks = {name: ex.fetch_tickers(FUTURES_TARGET_PAIRS) for name, ex in active_futures_exchanges.items()}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    
    prices_by_symbol = {}
    for (name, _), res in zip(tasks.items(), results):
        if isinstance(res, Exception):
            error_msg = f"{res}"
            if name not in connection_errors or connection_errors[name] != error_msg:
                await send_telegram_message(f"❌ *Erro de Conexão:* Falha ao buscar dados de `{name}`: `{error_msg}`")
                connection_errors[name] = error_msg
            elif name in connection_errors:
                 print(f"[AVISO] Erro de conexão com {name} persiste: {error_msg}")
            continue

        if name in connection_errors:
            await send_telegram_message(f"✅ *Erro Corrigido:* A conexão com `{name}` foi restabelecida.")
            del connection_errors[name]

        for symbol, ticker in res.items():
            if symbol not in prices_by_symbol: prices_by_symbol[symbol] = []
            if ticker.get('bid') and ticker.get('ask'):
                prices_by_symbol[symbol].append({'exchange': name, 'bid': Decimal(ticker['bid']), 'ask': Decimal(ticker['ask'])})

    opportunities = []
    for symbol, prices in prices_by_symbol.items():
        if len(prices) < 2: continue
        best_ask = min(prices, key=lambda x: x['ask'])
        best_bid = max(prices, key=lambda x: x['bid'])
        if best_ask['exchange'] != best_bid['exchange']:
            profit_pct = ((best_bid['bid'] - best_ask['ask']) / best_ask['ask']) * 100
            if profit_pct > futures_min_profit_threshold:
                opportunities.append({
                    'symbol': symbol, 'buy_exchange': best_ask['exchange'], 'buy_price': best_ask['ask'],
                    'sell_exchange': best_bid['exchange'], 'sell_price': best_bid['bid'], 'profit_percent': profit_pct
                })
    return sorted(opportunities, key=lambda x: x['profit_percent'], reverse=True)

async def loop_bot_futures():
    global futures_running, futures_trades_executed, futures_trade_limit, futures_monitored_pairs_count
    
    if not ccxt: return
    await initialize_futures_exchanges()
    if not active_futures_exchanges:
        await send_telegram_message("⚠️ *Bot de Futuros não iniciado:* Nenhuma chave de API válida ou conexão falhou.")
        return
    await send_telegram_message(f"✅ *Bot de Futuros iniciado.* Exchanges ativas: `{', '.join(active_futures_exchanges.keys())}`")
    
    futures_monitored_pairs_count = len(FUTURES_TARGET_PAIRS)
    
    while True:
        if not futures_running:
            await asyncio.sleep(30)
            continue
        
        if futures_trade_limit > 0 and futures_trades_executed >= futures_trade_limit:
            futures_running = False
            await send_telegram_message(f"🛑 *Limite de trades alcançado:* Bot de futuros desativado após {futures_trade_limit} trades.")
            continue
        
        opportunities = await find_futures_opportunities()
        
        if opportunities:
            opp = opportunities[0]
            trade_amount_usd = await get_trade_amount(opp['buy_exchange'], opp['symbol'], is_triangular=False)
            
            if futures_dry_run:
                msg = (f"💸 *Oportunidade de Futuros (Simulada)*\n\n"
                       f"Par: `{opp['symbol']}`\n"
                       f"Comprar em: `{opp['buy_exchange'].upper()}` a `{opp['buy_price']}`\n"
                       f"Vender em: `{opp['sell_exchange'].upper()}` a `{opp['sell_price']}`\n"
                       f"Lucro Potencial: *`{opp['profit_percent']:.3f}%`*\n"
                       f"Volume (aproximado): `{trade_amount_usd:.2f}` USDT\n")
                await send_telegram_message(msg)
                futures_trades_executed += 1
            else:
                futures_trades_executed += 1
                pass
        await asyncio.sleep(90)

# ==============================================================================
# 5. LÓGICA DO TELEGRAM BOT (COMMAND HANDLERS)
# ==============================================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Olá! O CryptoAlerts bot está online. Use /ajuda para ver os comandos.")

async def ajuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ajuda_text = (
        "🤖 *Comandos do Bot:*\n\n"
        "`/status` - Vê o status atual dos bots e configurações.\n"
        "`/saldos` - Vê o saldo de todas as exchanges conectadas.\n"
        "`/setlucro <triangular> <futuros>` - Define o lucro mínimo (ex: `0.003 0.5`).\n"
        "`/setvolume <triangular> <futuros>` - Define o volume. Use `%` para porcentagem (ex: `100 2%`).\n"
        "`/setlimite <num_trades>` - Define o limite de trades para futuros (0 para ilimitado).\n"
   async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def get_volume_text(is_triangular):
        amount = triangular_trade_amount if is_triangular else futures_trade_amount
        is_perc = triangular_trade_amount_is_percentage if is_triangular else futures_trade_amount_is_percentage
        return f"`{amount}%` do saldo" if is_perc else f"`{amount}` USDT"

    status_text = (
        "📊 *Status Geral dos Bots*\n\n"
        f"**Arbitragem Triangular (OKX Spot):**\n"
        f"Status: `{'ATIVO' if triangular_running else 'DESATIVADO'}`\n"
        f"Modo: `{'SIMULAÇÃO' if triangular_simulate else 'REAL'}`\n"
        f"Lucro Mínimo: `{triangular_min_profit_threshold:.3%}`\n"
        f"Volume de Trade: {get_volume_text(True)}\n"
        f"Ciclos Monitorados: `{triangular_monitored_cycles_count}`\n\n"
        f"**Arbitragem de Futuros (Multi-Exchange):**\n"
        f"Status: `{'ATIVO' if futures_running else 'DESATIVADO'}`\n"
        f"Modo: `{'SIMULAÇÃO' if futures_dry_run else 'REAL'}`\n"
        f"Lucro Mínimo: `{futures_min_profit_threshold:.2f}%`\n"
        f"Volume de Trade: {get_volume_text(False)}\n"
        f"Pares Monitorados: `{len(FUTURES_TARGET_PAIRS)}`\n"
        f"Trades Executados: `{futures_trades_executed}` / `{'Ilimitado' if futures_trade_limit == 0 else futures_trade_limit}`\n"
        f"Exchanges Ativas: `{', '.join(active_futures_exchanges.keys())}`\n"
        f"Erros de Conexão Ativos: `{', '.join(connection_errors.keys()) if connection_errors else 'Nenhum'}`"
    )
    await update.message.reply_text(status_text, parse_mode="Markdown")

async def saldos_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ccxt or not active_futures_exchanges:
        await update.message.reply_text("Nenhuma exchange de futuros conectada.")
        return
    
    balances_text = "💰 *Saldos Atuais (USDT)*\n\n"
    for name, ex in active_futures_exchanges.items():
        try:
            balance = await ex.fetch_balance()
            total_usdt = Decimal(balance.get('total', {}).get('USDT', 0))
            free_usdt = Decimal(balance.get('free', {}).get('USDT', 0))
            balances_text += f"*{name.upper()}*: `Total: {total_usdt:.2f}` | `Disponível: {free_usdt:.2f}`\n"
        except Exception as e:
            balances_text += f"*{name.upper()}*: Erro ao carregar saldo. `{e}`\n"
    await update.message.reply_text(balances_text, parse_mode="Markdown")

async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_min_profit_threshold, futures_min_profit_threshold
    try:
        args = context.args
        if len(args) != 2:
            await update.message.reply_text("Uso: `/setlucro <triangular> <futuros>`\n(Ex: `0.003 0.5`)")
            return
        triangular_min_profit_threshold = Decimal(args[0])
        futures_min_profit_threshold = Decimal(args[1])
        await update.message.reply_text(f"Lucro mínimo atualizado: Triangular `{triangular_min_profit_threshold:.3%}` | Futuros `{futures_min_profit_threshold:.2f}%`")
    except Exception:
        await update.message.reply_text("Valores inválidos.")

async def setvolume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_trade_amount, triangular_trade_amount_is_percentage
    global futures_trade_amount, futures_trade_amount_is_percentage
    try:
        args = context.args
        if len(args) != 2:
            await update.message.reply_text("Uso: `/setvolume <triangular> <futuros>`\n(Ex: `50` ou `2%`)")
            return

        def parse_volume_arg(arg_str):
            if arg_str.endswith('%'):
                return Decimal(arg_str[:-1]), True
            return Decimal(arg_str), False

        tri_vol, tri_is_perc = parse_volume_arg(args[0])
        fut_vol, fut_is_perc = parse_volume_arg(args[1])

        triangular_trade_amount, triangular_trade_amount_is_percentage = tri_vol, tri_is_perc
        futures_trade_amount, futures_trade_amount_is_percentage = fut_vol, fut_is_perc
        
        tri_text = f"`{tri_vol}%` do saldo" if tri_is_perc else f"`{tri_vol}` USDT"
        fut_text = f"`{fut_vol}%` do saldo" if fut_is_perc else f"`{fut_vol}` USDT"

        await update.message.reply_text(f"Volume de trade atualizado:\nTriangular: {tri_text}\nFuturos: {fut_text}")
    except Exception:
        await update.message.reply_text("Valores inválidos.")

async def setlimite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global futures_trade_limit, futures_trades_executed
    try:
        if not context.args:
            await update.message.reply_text(f"Limite atual: `{'Ilimitado' if futures_trade_limit == 0 else futures_trade_limit}`. Trades: `{futures_trades_executed}`\n\nUso: `/setlimite <num>` (0 para ilimitado).", parse_mode="Markdown")
            return
        
        limit = int(context.args[0])
        if limit < 0:
            await update.message.reply_text("O limite deve ser >= 0.", parse_mode="Markdown")
            return
            
        futures_trade_limit = limit
        futures_trades_executed = 0
      async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def get_volume_text(is_triangular):
        amount = triangular_trade_amount if is_triangular else futures_trade_amount
        is_perc = triangular_trade_amount_is_percentage if is_triangular else futures_trade_amount_is_percentage
        return f"`{amount}%` do saldo" if is_perc else f"`{amount}` USDT"

    status_text = (
        "📊 *Status Geral dos Bots*\n\n"
        f"**Arbitragem Triangular (OKX Spot):**\n"
        f"Status: `{'ATIVO' if triangular_running else 'DESATIVADO'}`\n"
        f"Modo: `{'SIMULAÇÃO' if triangular_simulate else 'REAL'}`\n"
        f"Lucro Mínimo: `{triangular_min_profit_threshold:.3%}`\n"
        f"Volume de Trade: {get_volume_text(True)}\n"
        f"Ciclos Monitorados: `{triangular_monitored_cycles_count}`\n\n"
        f"**Arbitragem de Futuros (Multi-Exchange):**\n"
        f"Status: `{'ATIVO' if futures_running else 'DESATIVADO'}`\n"
        f"Modo: `{'SIMULAÇÃO' if futures_dry_run else 'REAL'}`\n"
        f"Lucro Mínimo: `{futures_min_profit_threshold:.2f}%`\n"
        f"Volume de Trade: {get_volume_text(False)}\n"
        f"Pares Monitorados: `{len(FUTURES_TARGET_PAIRS)}`\n"
        f"Trades Executados: `{futures_trades_executed}` / `{'Ilimitado' if futures_trade_limit == 0 else futures_trade_limit}`\n"
        f"Exchanges Ativas: `{', '.join(active_futures_exchanges.keys())}`\n"
        f"Erros de Conexão Ativos: `{', '.join(connection_errors.keys()) if connection_errors else 'Nenhum'}`"
    )
    await update.message.reply_text(status_text, parse_mode="Markdown")

async def saldos_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ccxt or not active_futures_exchanges:
        await update.message.reply_text("Nenhuma exchange de futuros conectada.")
        return
    
    balances_text = "💰 *Saldos Atuais (USDT)*\n\n"
    for name, ex in active_futures_exchanges.items():
        try:
            balance = await ex.fetch_balance()
            total_usdt = Decimal(balance.get('total', {}).get('USDT', 0))
            free_usdt = Decimal(balance.get('free', {}).get('USDT', 0))
            balances_text += f"*{name.upper()}*: `Total: {total_usdt:.2f}` | `Disponível: {free_usdt:.2f}`\n"
        except Exception as e:
            balances_text += f"*{name.upper()}*: Erro ao carregar saldo. `{e}`\n"
    await update.message.reply_text(balances_text, parse_mode="Markdown")

async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_min_profit_threshold, futures_min_profit_threshold
    try:
        args = context.args
        if len(args) != 2:
            await update.message.reply_text("Uso: `/setlucro <triangular> <futuros>`\n(Ex: `0.003 0.5`)")
            return
        triangular_min_profit_threshold = Decimal(args[0])
        futures_min_profit_threshold = Decimal(args[1])
        await update.message.reply_text(f"Lucro mínimo atualizado: Triangular `{triangular_min_profit_threshold:.3%}` | Futuros `{futures_min_profit_threshold:.2f}%`")
    except Exception:
        await update.message.reply_text("Valores inválidos.")

async def setvolume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_trade_amount, triangular_trade_amount_is_percentage
    global futures_trade_amount, futures_trade_amount_is_percentage
    try:
        args = context.args
        if len(args) != 2:
            await update.message.reply_text("Uso: `/setvolume <triangular> <futuros>`\n(Ex: `50` ou `2%`)")
            return

        def parse_volume_arg(arg_str):
            if arg_str.endswith('%'):
                return Decimal(arg_str[:-1]), True
            return Decimal(arg_str), False

        tri_vol, tri_is_perc = parse_volume_arg(args[0])
        fut_vol, fut_is_perc = parse_volume_arg(args[1])

        triangular_trade_amount, triangular_trade_amount_is_percentage = tri_vol, tri_is_perc
        futures_trade_amount, futures_trade_amount_is_percentage = fut_vol, fut_is_perc
        
        tri_text = f"`{tri_vol}%` do saldo" if tri_is_perc else f"`{tri_vol}` USDT"
        fut_text = f"`{fut_vol}%` do saldo" if fut_is_perc else f"`{fut_vol}` USDT"

        await update.message.reply_text(f"Volume de trade atualizado:\nTriangular: {tri_text}\nFuturos: {fut_text}")
    except Exception:
        await update.message.reply_text("Valores inválidos.")

async def setlimite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global futures_trade_limit, futures_trades_executed
    try:
        if not context.args:
            await update.message.reply_text(f"Limite atual: `{'Ilimitado' if futures_trade_limit == 0 else futures_trade_limit}`. Trades: `{futures_trades_executed}`\n\nUso: `/setlimite <num>` (0 para ilimitado).", parse_mode="Markdown")
            return
        
        limit = int(context.args[0])
        if limit < 0:
            await update.message.reply_text("O limite deve ser >= 0.", parse_mode="Markdown")
            return
            
        futures_trade_limit = limit
        futures_trades_executed = 0
        
        limit_text = f"`{futures_trade_limit}` trades" if futures_trade_limit > 0 else "Ilimitado"
        await update.message.reply_text(f"Limite de trades para futuros definido para: {limit_text}. Contador resetado.", parse_mode="Markdown")
    except (ValueError, IndexError):
        await update.message.reply_text("Valor inválido. Use `/setlimite <número>`.", parse_mode="Markdown")

async def setalavancagem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ccxt:
        await update.message.reply_text("Erro: Módulo 'ccxt' não disponível.")
        return
    try:
        args = context.args
        if len(args) != 3:
            await update.message.reply_text("Uso: `/setalavancagem <exchange> <par> <valor>`\nEx: `/setalavancagem okx BTC/USDT:USDT 20`", parse_mode="Markdown")
            return
        
        exchange_name, symbol, leverage_str = args[0].lower(), args[1], args[2]
        leverage = int(leverage_str)
        
        if exchange_name not in active_futures_exchanges:
            await update.message.reply_text(f"Exchange `{exchange_name}` não está conectada ou é inválida.")
            return

        exchange = active_futures_exchanges[exchange_name]
        await update.message.reply_text(f"Tentando definir alavancagem de `{symbol}` para `{leverage}x` em `{exchange_name.upper()}`...")
        
        try:
            await exchange.set_leverage(leverage, symbol, params={'mgnMode': 'cross'})
            await update.message.reply_text(f"✅ Alavancagem de `{symbol}` em `{exchange_name.upper()}` definida para `{leverage}x` com sucesso!")
        except Exception as e:
            await update.message.reply_text(f"❌ Falha ao definir alavancagem: `{e}`")
            
    except (ValueError, IndexError):
        await update.message.reply_text("Valores inválidos. Verifique se a alavancagem é um número inteiro.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Erro ao processar o comando: `{e}`")

async def ligar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_running, futures_running
    try:
        bot_name = context.args[0].lower()
        if bot_name == 'triangular':
            triangular_running = True
            await update.message.reply_text("✅ Bot triangular ATIVADO.")
        elif bot_name == 'futuros':
            futures_running = True
            await update.message.reply_text("✅ Bot de futuros ATIVADO.")
        else:
            await update.message.reply_text("Bot inválido. Use 'triangular' ou 'futuros'.")
    except IndexError:
        await update.message.reply_text("Uso: `/ligar <bot>` (triangular ou futuros)", parse_mode="Markdown")

async def desligar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_running, futures_running
    try:
        bot_name = context.args[0].lower()
        if bot_name == 'triangular':
            triangular_running = False
            await update.message.reply_text("🛑 Bot triangular DESATIVADO.")
        elif bot_name == 'futuros':
            futures_running = False
            await update.message.reply_text("🛑 Bot de futuros DESATIVADO.")
        else:
            await update.message.reply_text("Bot inválido. Use 'triangular' ou 'futuros'.")
    except IndexError:
        await update.message.reply_text("Uso: `/desligar <bot>` (triangular ou futuros)", parse_mode="Markdown")

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Comando desconhecido. Use `/ajuda` para ver os comandos válidos.")

# ==============================================================================
# 6. INICIALIZAÇÃO E LOOP PRINCIPAL
# ==============================================================================
async def main():
    """Roda o bot e os loops de arbitragem."""
    print("[INFO] Iniciando o bot...")
    
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("ajuda", ajuda_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("saldos", saldos_command))
    application.add_handler(CommandHandler("setlucro", setlucro_command))
    application.add_handler(CommandHandler("setvolume", setvolume_command))
    application.add_handler(CommandHandler("setlimite", setlimite_command))
    application.add_handler(CommandHandler("setalavancagem", setalavancagem_command))
    application.add_handler(CommandHandler("ligar", ligar_command))
    application.add_handler(CommandHandler("desligar", desligar_command))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    init_triangular_db()
    
    asyncio.create_task(loop_bot_triangular())
    if ccxt:
        asyncio.create_task(loop_bot_futures())
    
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        await send_telegram_message("✅ *Bot iniciado e online!* Use /status para verificar.")

    print("[INFO] Bot do Telegram rodando. Pressione Ctrl+C para parar.")
    
    await application.run_polling()

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nBot encerrado pelo usuário.")
    finally:
        print("Finalizando todas as tarefas...")
        tasks = asyncio.all_tasks(loop=loop)
        for task in tasks:
            task.cancel()
        
        async def gather_tasks():
            await asyncio.gather(*tasks, return_exceptions=True)

        loop.run_until_complete(gather_tasks())
        loop.close()
  
        limit_text = f"`{futures_trade_limit}` trades" if futures_trade_limit > 0 else "Ilimitado"
        await update.message.reply_text(f"Limite de trades para futuros definido para: {limit_text}. Contador resetado.", parse_mode="Markdown")
    except (ValueError, IndexError):
        await update.message.reply_text("Valor inválido. Use `/setlimite <número>`.", parse_mode="Markdown")

async def setalavancagem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ccxt:
        await update.message.reply_text("Erro: Módulo 'ccxt' não disponível.")
        return
    try:
        args = context.args
        if len(args) != 3:
            await update.message.reply_text("Uso: `/setalavancagem <exchange> <par> <valor>`\nEx: `/setalavancagem okx BTC/USDT
     "`/setalavancagem <ex> <par> <val>` - Ajusta a alavancagem (ex: `okx BTC/USDT:USDT 20`).\n"
        "`/ligar <bot>` - Liga um bot (`triangular` ou `futuros`).\n"
        "`/desligar <bot>` - Desliga um bot (`triangular` ou `futuros`).\n"
    )
    await update.message.reply_text(ajuda_text, parse_mode="Markdown")
     await asyncio.sleep(20)
