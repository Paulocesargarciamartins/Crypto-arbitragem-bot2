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

# --- Configuração de Arquivo Dinâmico ---
CONFIG_FILE = "/tmp/bot_config.json"

# --- Funções de Configuração Dinâmica ---
def load_config():
    default_config = {
        "active_exchanges": ["gateio", "mexc", "bitget"],
        "target_pairs": [
            'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'BNB/USDT:USDT', 'XRP/USDT:USDT', 'DOGE/USDT:USDT', 'ADA/USDT:USDT', 'AVAX/USDT:USDT',
            'LINK/USDT:USDT', 'MATIC/USDT:USDT', 'LTC/USDT:USDT', 'NEAR/USDT:USDT', 'ATOM/USDT:USDT', 'UNI/USDT:USDT', 'OP/USDT:USDT', 'ARB/USDT:USDT',
            'DOT/USDT:USDT', 'TRX/USDT:USDT', 'SHIB/USDT:USDT', 'APT/USDT:USDT', 'FIL/USDT:USDT', 'AAVE/USDT:USDT', 'RUNE/USDT:USDT', 'FTM/USDT:USDT',
            'PEPE/USDT:USDT', 'SUI/USDT:USDT', 'ICP/USDT:USDT', 'GRT/USDT:USDT', 'DYDX/USDT:USDT', 'RNDR/USDT:USDT', 'INJ/USDT:USDT', 'WLD/USDT:USDT',
            'ORDI/USDT:USDT', 'TIA/USDT:USDT', 'KAS/USDT:USDT', 'TON/USDT:USDT', 'BONK/USDT:USDT', 'FLOKI/USDT:USDT', '1000SATS/USDT:USDT',
            'XLM/USDT:USDT', 'ALGO/USDT:USDT', 'VET/USDT:USDT', 'IMX/USDT:USDT', 'MKR/USDT:USDT', 'CRV/USDT:USDT', 'SNX/USDT:USDT', '1INCH/USDT:USDT'
        ]
    }
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f: return json.load(f)
        else:
            with open(CONFIG_FILE, 'w') as f: json.dump(default_config, f, indent=4)
            return default_config
    except (IOError, json.JSONDecodeError): return default_config

def save_config(config):
    try:
        with open(CONFIG_FILE, 'w') as f: json.dump(config, f, indent=4)
    except IOError: print(f"[ERRO] Não foi possível salvar o arquivo de configuração em {CONFIG_FILE}")

bot_config = load_config()

API_KEYS_FUTURES = {
    'gateio': {'apiKey': os.getenv('GATEIO_API_KEY'), 'secret': os.getenv('GATEIO_API_SECRET')},
    'mexc': {'apiKey': os.getenv('MEXC_API_KEY'), 'secret': os.getenv('MEXC_API_SECRET')},
    'bitget': {'apiKey': os.getenv('BITGET_API_KEY'), 'secret': os.getenv('BITGET_API_SECRET'), 'password': os.getenv('BITGET_API_PASSPHRASE')},
    'bitrue': {'apiKey': os.getenv('BITRUE_API_KEY'), 'secret': os.getenv('BITRUE_API_SECRET')},
}

try:
    import ccxt.async_support as ccxt
except ImportError: ccxt = None

triangular_running, futures_running = True, True
triangular_paused, futures_paused = False, False
last_opportunity_check_time = "N/A"
triangular_min_profit_threshold = Decimal(os.getenv("MIN_PROFIT_THRESHOLD", "0.002"))
futures_min_profit_threshold = Decimal(os.getenv("FUTURES_MIN_PROFIT_THRESHOLD", "0.01"))
triangular_simulate = os.getenv("TRIANGULAR_SIMULATE", "false").lower() in ["1", "true", "yes"]
futures_dry_run = os.getenv("FUTURES_DRY_RUN", "true").lower() in ["1", "true", "yes"]
futures_trade_limit = int(os.getenv("FUTURES_TRADE_LIMIT", "0"))
futures_trades_executed = 0
triangular_trade_amount = Decimal("1")
triangular_trade_amount_is_percentage = False
futures_trade_amount = Decimal(os.getenv("FUTURES_TRADE_AMOUNT_USDT", "10"))
futures_trade_amount_is_percentage = False

# ==============================================================================
# 2. FUNÇÕES AUXILIARES GLOBAIS
# ==============================================================================
async def send_telegram_message(text, chat_id=None, update: Update = None):
    final_chat_id = chat_id or (update.effective_chat.id if update else TELEGRAM_CHAT_ID)
    if not TELEGRAM_TOKEN or not final_chat_id: return
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        await bot.send_message(chat_id=final_chat_id, text=text, parse_mode="Markdown")
    except Exception as e: print(f"Erro ao enviar mensagem no Telegram: {e}")

async def get_okx_spot_balances(symbols_list):
    if not ccxt: return "Erro: CCXT não disponível."
    try:
        okx_creds = {'apiKey': OKX_API_KEY, 'secret': OKX_API_SECRET, 'password': OKX_API_PASSPHRASE}
        if not all(okx_creds.values()): return "Chaves da OKX não configuradas."
        exchange = ccxt.okx(okx_creds)
        balance = await exchange.fetch_balance()
        await exchange.close()
        balances_text = ""
        for symbol in symbols_list:
            free_balance = balance.get('free', {}).get(symbol.upper(), 0)
            balances_text += f"{symbol.upper()}: `{Decimal(free_balance):.4f}` "
        return balances_text.strip()
    except Exception as e:
        print(f"[ERRO-SALDO-OKX] {e}")
        return "Erro ao buscar saldos."

async def get_futures_leverage_for_symbol(exchange_name, symbol):
    if not ccxt or exchange_name not in active_futures_exchanges: return Decimal(1)
    ex = active_futures_exchanges[exchange_name]
    try:
        position = await ex.fetch_position(symbol)
        return Decimal(position['leverage'])
    except Exception:
        try:
            tiers = await ex.fetch_leverage_tiers([symbol])
            if tiers and symbol in tiers and tiers[symbol]: return Decimal(tiers[symbol][0]['leverage'])
        except Exception: pass
    return Decimal(1)

async def get_trade_amount(exchange_name, symbol, is_triangular):
    amount_value = triangular_trade_amount if is_triangular else futures_trade_amount
    is_percentage = triangular_trade_amount_is_percentage if is_triangular else futures_trade_amount_is_percentage
    if not is_percentage: return amount_value
    try:
        if not ccxt: return amount_value
        ex = active_futures_exchanges.get(exchange_name)
        if not ex: return amount_value
        balance = await ex.fetch_balance()
        available_usdt = Decimal(balance.get('free', {}).get('USDT', 0))
        if available_usdt == 0: raise ValueError("Saldo USDT é zero.")
        calculated_amount = available_usdt * (amount_value / 100)
        if not is_triangular:
            leverage = await get_futures_leverage_for_symbol(exchange_name, symbol)
            if leverage > 0: calculated_amount *= leverage
            else: raise ValueError("Alavancagem não encontrada.")
        return calculated_amount
    except Exception as e:
        await send_telegram_message(f"⚠️ *Erro ao calcular volume:* `{e}`. Usando valor padrão: `{amount_value}` USDT.")
        return amount_value

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
        c.execute("CREATE TABLE IF NOT EXISTS ciclos (timestamp TEXT, pares TEXT, lucro_percent REAL, lucro_usdt REAL, modo TEXT, status TEXT, detalhes TEXT)")
        conn.commit()

def registrar_ciclo_triangular(pares, lucro_percent, lucro_usdt, modo, status, detalhes=""):
    global triangular_lucro_total_usdt
    triangular_lucro_total_usdt += Decimal(str(lucro_usdt))
    with sqlite3.connect(TRIANGULAR_DB_FILE, check_same_thread=False) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO ciclos VALUES (?, ?, ?, ?, ?, ?, ?)", (datetime.now(timezone.utc).isoformat(), json.dumps(pares), float(lucro_percent), float(lucro_usdt), modo, status, detalhes))
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
        if quote_ccy not in pairs_by_quote: pairs_by_quote[quote_ccy] = []
        pairs_by_quote[quote_ccy].append(inst)
    cycles = []
    if 'USDT' in pairs_by_quote:
        for pair1 in pairs_by_quote['USDT']:
            base1 = pair1['baseCcy']
            for pivot in main_currencies:
                if base1 == pivot: continue
                for pair2 in pairs_by_quote.get(pivot, []):
                    if pair2['baseCcy'] == base1:
                        cycles.append([(f"{base1}-USDT", "buy"), (f"{base1}-{pivot}", "sell"), (f"{pivot}-USDT", "sell")])
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
    if amt == 0: return Decimal("0"), Decimal("0")
    start_amt = amt
    for instId, action in cycle:
        ticker = tickers.get(instId)
        if not ticker: raise RuntimeError(f"Ticker para {instId} não encontrado.")
        price = ticker["ask"] if action == "buy" else ticker["bid"]
        fee = amt * TRIANGULAR_FEE_RATE
        if action == "buy": amt = (amt - fee) / price
        else: amt = (amt * price) - fee
    profit_abs = amt - start_amt
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
        print(f"[ERRO-CRÍTICO-TRIANGULAR] Falha ao construir ciclos: {e}")
        return

    while True:
        if not triangular_running or triangular_paused:
            await asyncio.sleep(30)
            continue
        try:
            all_inst_ids = list({instId for cycle in dynamic_cycles for instId, _ in cycle})
            all_tickers = get_okx_spot_tickers(all_inst_ids)
            for cycle in dynamic_cycles:
                try:
                    profit_pct, profit_abs = await simulate_triangular_cycle(cycle, all_tickers)
                    if profit_pct > triangular_min_profit_threshold:
                        pares_fmt = " → ".join([p for p, a in cycle])
                        involved_currencies = list(set(sum([p.split('-') for p, a in cycle], [])))
                        if triangular_simulate:
                            msg = (f"🚀 *Oportunidade Triangular (Simulada)*\n\n"
                                   f"`{pares_fmt}`\n"
                                   f"Lucro Previsto: `{profit_pct:.3%}` (~`{profit_abs:.4f} USDT`)")
                            registrar_ciclo_triangular(pares_fmt, float(profit_pct), float(profit_abs), "SIMULATE", "OK")
                            await send_telegram_message(msg)
                        else:
                            saldos_atuais = await get_okx_spot_balances(involved_currencies)
                            msg = (f"✅ *Arbitragem Triangular (Finalizada)*\n\n"
                                   f"`{pares_fmt}`\n"
                                   f"Lucro Real: `{profit_pct:.3%}` (~`{profit_abs:.4f} USDT`)\n"
                                   f"Saldos Finais: `{saldos_atuais}`")
                            registrar_ciclo_triangular(pares_fmt, float(profit_pct), float(profit_abs), "LIVE", "OK")
                            await send_telegram_message(msg)
                except Exception: pass
        except Exception as e: print(f"[ERRO-LOOP-TRIANGULAR] {e}")
        await asyncio.sleep(20)

# ==============================================================================
# 4. MÓDULO DE ARBITRAGEM DE FUTUROS (MULTI-EXCHANGE)
# ==============================================================================
active_futures_exchanges = {}

async def initialize_futures_exchanges():
    global active_futures_exchanges
    if not ccxt: return
    print("[INFO] Inicializando exchanges para MODO FUTUROS...")
    active_futures_exchanges.clear()
    current_active_exchanges = bot_config.get("active_exchanges", [])
    for name in current_active_exchanges:
        creds = API_KEYS_FUTURES.get(name)
        if not creds or not creds.get('apiKey'):
            print(f"[AVISO] Chaves para '{name}' não encontradas. Pulando.")
            continue
        try:
            exchange_class = getattr(ccxt, name)
            instance = exchange_class({**creds, 'options': {'defaultType': 'swap'}})
            await instance.load_markets()
            active_futures_exchanges[name] = instance
            print(f"[INFO-FUTUROS] Exchange '{name}' carregada.")
        except Exception as e:
            print(f"[ERRO-FUTUROS] Falha ao instanciar '{name}': {e}")
            await send_telegram_message(f"❌ *Erro de Conexão:* Falha ao conectar em `{name}`: `{e}`")

async def find_futures_opportunities():
    global last_opportunity_check_time
    last_opportunity_check_time = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    target_pairs = bot_config.get("target_pairs", [])
    if not target_pairs or not active_futures_exchanges: return []
    
    tasks = {name: ex.fetch_tickers(target_pairs) for name, ex in active_futures_exchanges.items()}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    
    prices_by_symbol = {}
    for (name, _), res in zip(tasks.items(), results):
        if isinstance(res, Exception): continue
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
                opportunities.append({'symbol': symbol, 'buy_exchange': best_ask['exchange'], 'buy_price': best_ask['ask'],
                                      'sell_exchange': best_bid['exchange'], 'sell_price': best_bid['bid'], 'profit_percent': profit_pct})
    return sorted(opportunities, key=lambda x: x['profit_percent'], reverse=True)

async def loop_bot_futures():
    global futures_running, futures_trades_executed, futures_trade_limit, futures_paused
    if not ccxt: return
    await initialize_futures_exchanges()
    if not active_futures_exchanges:
        await send_telegram_message("⚠️ *Bot de Futuros não iniciado:* Nenhuma exchange ativa ou chaves válidas.")
        return
    await send_telegram_message(f"✅ *Bot de Futuros iniciado.* Exchanges: `{', '.join(active_futures_exchanges.keys())}`")

    while True:
        if not futures_running or futures_paused:
            await asyncio.sleep(30)
            continue
        if futures_trade_limit > 0 and futures_trades_executed >= futures_trade_limit:
            futures_running = False
            await send_telegram_message(f"🛑 *Limite de trades alcançado:* Bot de futuros desativado.")
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
                       f"Volume (aprox): `{trade_amount_usd:.2f}` USDT")
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
    await update.message.reply_text("Olá! Bot CryptoAlerts online. Use /ajuda para ver os comandos.")

async def ajuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ajuda_text = (
        "🤖 *Comandos do Bot:*\n\n"
        "*GERAL:*\n"
        "`/status` - Status geral dos bots.\n"
        "`/ajuda` - Mostra esta mensagem.\n\n"
        "*CONTROLE DE BOTS:*\n"
        "`/ligar <bot>` - Liga um bot (`triangular` ou `futuros`).\n"
        "`/desligar <bot>` - Desliga um bot.\n"
        "`/pausar <bot>` - Pausa trades (continua analisando).\n"
        "`/retomar <bot>` - Retoma trades.\n\n"
        "*CONFIGURAÇÕES:*\n"
        "`/setlucro <tri> <fut>` - Define lucro mínimo (ex: `0.002 0.01`).\n"
        "`/setvolume <tri> <fut>` - Define volume (ex: `50 2%`).\n"
        "`/setlimite <num>` - Limite de trades de futuros (0=infinito).\n\n"
        "*FERRAMENTAS:*\n"
        "`/radar <PAR>` - Vê preços de um par em tempo real.\n"
        "`/saldos` - Vê saldos de futuros em USDT.\n\n"
        "*GERENCIAMENTO (FUTUROS):*\n"
        "`/listex` - Lista exchanges ativas.\n"
        "`/addex <nome>` - Adiciona uma exchange.\n"
        "`/resex <nome>` - Remove uma exchange.\n"
        "`/listpar` - Lista pares monitorados.\n"
        "`/addpar <PAR>` - Adiciona um par.\n"
        "`/respar <PAR>` - Remove um par."
    )
    await update.message.reply_text(ajuda_text, parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    def get_bot_status(is_running, is_paused):
        if not is_running: return 'DESATIVADO'
        if is_paused: return 'PAUSADO'
        return 'ATIVO'
    
    futures_leverage_text = ""
    if active_futures_exchanges:
        tasks = {name: get_futures_leverage_for_symbol(name, 'BTC/USDT:USDT') for name in active_futures_exchanges.keys()}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for (name, _), res in zip(tasks.items(), results):
            lev_text = f"{res}x" if isinstance(res, (int, float, Decimal)) else "Erro"
            futures_leverage_text += f" | {name.upper()}: `{lev_text}`"

    def get_volume_text(is_triangular):
        amount = triangular_trade_amount if is_triangular else futures_trade_amount
        is_perc = triangular_trade_amount_is_percentage if is_triangular else futures_trade_amount_is_percentage
        return f"`{amount}%` da banca" if is_perc else f"`{amount}` USDT"

    status_text = (
        f"📊 *Status Geral dos Bots*\n\n"
        f"**Arbitragem Triangular (OKX Spot):**\n"
        f"Status: `{get_bot_status(triangular_running, triangular_paused)}`\n"
        f"Modo: `{'SIMULAÇÃO' if triangular_simulate else 'REAL'}`\n"
        f"Lucro Mínimo: `{triangular_min_profit_threshold:.3%}`\n"
        f"Volume: {get_volume_text(True)}\n"
        f"Ciclos Monitorados: `{triangular_monitored_cycles_count}`\n"
        f"Lucro Total: `{triangular_lucro_total_usdt:.4f} USDT`\n\n"
        f"**Arbitragem de Futuros (Multi-Exchange):**\n"
        f"Status: `{get_bot_status(futures_running, futures_paused)}`\n"
        f"Modo: `{'SIMULAÇÃO' if futures_dry_run else 'REAL'}`\n"
        f"Lucro Mínimo: `{futures_min_profit_threshold:.2f}%`\n"
        f"Volume: {get_volume_text(False)}\n"
        f"Pares Monitorados: `{len(bot_config.get('target_pairs', []))}`\n"
        f"Trades Executados: `{futures_trades_executed}` / `{'Ilimitado' if futures_trade_limit == 0 else futures_trade_limit}`\n"
        f"Exchanges Ativas: `{', '.join(bot_config.get('active_exchanges', []))}`\n"
        f"Última Verificação: `{last_opportunity_check_time}`\n"
        f"Alavancagem (BTC/USDT):{futures_leverage_text}"
    )
    await update.message.reply_text(status_text, parse_mode="Markdown")

async def saldos_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ccxt or not active_futures_exchanges:
        await update.message.reply_text("Nenhuma exchange de futuros conectada.")
        return
    balances_text = "💰 *Saldos de Futuros (USDT)*\n\n"
    for name, ex in active_futures_exchanges.items():
        try:
            balance = await ex.fetch_balance()
            total_usdt = Decimal(balance.get('total', {}).get('USDT', 0))
            free_usdt = Decimal(balance.get('free', {}).get('USDT', 0))
            balances_text += f"*{name.upper()}*: Total `{total_usdt:.2f}`, Disp `{free_usdt:.2f}`\n"
        except Exception as e:
            balances_text += f"*{name.upper()}*: Erro ao carregar saldo.\n"
    await update.message.reply_text(balances_text, parse_mode="Markdown")

async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_min_profit_threshold, futures_min_profit_threshold
    try:
        tri_profit, fut_profit = Decimal(context.args[0]), Decimal(context.args[1])
        triangular_min_profit_threshold, futures_min_profit_threshold = tri_profit, fut_profit
        await update.message.reply_text(f"Lucro mínimo: Triangular `{tri_profit:.3%}` | Futuros `{fut_profit:.2f}%`")
    except (ValueError, IndexError):
        await update.message.reply_text("Uso: `/setlucro <triangular> <futuros>`")

async def setvolume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_trade_amount, triangular_trade_amount_is_percentage, futures_trade_amount, futures_trade_amount_is_percentage
    try:
        def parse_arg(arg):
            is_perc = '%' in arg
            val = Decimal(arg.replace('%', ''))
            return val, is_perc
        tri_vol, tri_is_perc = parse_arg(context.args[0])
        fut_vol, fut_is_perc = parse_arg(context.args[1])
        triangular_trade_amount, triangular_trade_amount_is_percentage = tri_vol, tri_is_perc
        futures_trade_amount, futures_trade_amount_is_percentage = fut_vol, fut_is_perc
        await update.message.reply_text("Volume de trade atualizado.")
    except (ValueError, IndexError):
        await update.message.reply_text("Uso: `/setvolume <triangular> <futuros>` (ex: `50 2%`)")

async def setlimite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global futures_trade_limit, futures_trades_executed
    try:
        limit = int(context.args[0])
        if limit < 0: raise ValueError("Limite deve ser >= 0")
        futures_trade_limit = limit
        futures_trades_executed = 0
        await update
