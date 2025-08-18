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
triangular_simulate = os.getenv("TRIANGULAR_SIMULATE", "true").lower() in ["1", "true", "yes"]
futures_dry_run = os.getenv("FUTURES_DRY_RUN", "true").lower() in ["1", "true", "yes"]
futures_trade_limit = int(os.getenv("FUTURES_TRADE_LIMIT", "0"))
futures_trades_executed = 0

# --- Configurações de Volume de Trade ---
triangular_trade_amount = Decimal(os.getenv("TRADE_AMOUNT_USDT", "10"))
triangular_trade_amount_is_percentage = False
futures_trade_amount = Decimal(os.getenv("FUTURES_TRADE_AMOUNT_USDT", "10"))
futures_trade_amount_is_percentage = False

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

async def get_futures_leverage_for_symbol(exchange_name, symbol):
    if not ccxt or exchange_name not in active_futures_exchanges: return Decimal(1)
    ex = active_futures_exchanges[exchange_name]
    try:
        position = await ex.fetch_position(symbol)
        return Decimal(position['leverage'])
    except Exception:
        try:
            leverage_tiers = await ex.fetch_leverage_tiers([symbol])
            if leverage_tiers and symbol in leverage_tiers and leverage_tiers[symbol]:
                return Decimal(leverage_tiers[symbol][0]['leverage'])
        except Exception:
            pass
    return Decimal(1)

async def get_trade_amount(exchange_name, symbol, is_triangular):
    amount_value = triangular_trade_amount if is_triangular else futures_trade_amount
    is_percentage = triangular_trade_amount_is_percentage if is_triangular else futures_trade_amount_is_percentage

    if not is_percentage:
        return amount_value

    try:
        if not ccxt: return amount_value
        if exchange_name not in active_futures_exchanges: return amount_value
        
        ex = active_futures_exchanges[exchange_name]
        
        balance = await ex.fetch_balance()
        available_usdt = Decimal(balance.get('free', {}).get('USDT', 0))
        if available_usdt == 0:
            raise ValueError("Saldo em USDT é zero. Não é possível calcular o volume.")

        calculated_amount = available_usdt * (amount_value / 100)
        
        if not is_triangular:
            leverage = await get_futures_leverage_for_symbol(exchange_name, symbol)
            if leverage > 0:
                calculated_amount *= leverage
            else:
                raise ValueError("Alavancagem do par não encontrada ou é zero.")
        
        return calculated_amount

    except Exception as e:
        await send_telegram_message(f"⚠️ *Erro ao obter saldo/alavancagem para calcular volume:* `{e}`. Usando valor padrão: `{amount_value}` USDT.")
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
    if amt == 0:
        return Decimal("0"), Decimal("0")
    start_amt = amt
    for instId, action in cycle:
        ticker = tickers.get(instId)
        if not ticker: raise RuntimeError(f"Ticker para {instId} não encontrado durante a simulação.")
        price = ticker["ask"] if action == "buy" else ticker["bid"]
        fee = amt * TRIANGULAR_FEE_RATE
        if action == "buy":
            amt = (amt - fee) / price
        elif action == "sell":
            amt = (amt * price) - fee
    final_usdt = amt
    profit_abs = final_usdt - start_amt
    profit_pct = profit_abs / start_amt if start_amt > 0 else 0
    return profit_pct, profit_abs

async def loop_bot_triangular():
    global triangular_monitored_cycles_count
    print("[INFO] Bot de Arbitragem Triangular (OKX Spot) iniciado.")
    try:
        print("[INFO-TRIANGULAR] Buscando todos os instrumentos da OKX para construir ciclos dinâmicos...")
        all_instruments = get_all_okx_spot_instruments()
        dynamic_cycles = build_dynamic_cycles(all_instruments)
        triangular_monitored_cycles_count = len(dynamic_cycles)
        print(f"[INFO-TRIANGULAR] {triangular_monitored_cycles_count} ciclos de arbitragem foram construídos dinamicamente.")
        if triangular_monitored_cycles_count == 0:
            await send_telegram_message("⚠️ *Aviso Triangular:* Nenhum ciclo de arbitragem pôde ser construído.")
    except Exception as e:
        print(f"[ERRO-CRÍTICO-TRIANGULAR] Falha ao construir ciclos dinâmicos: {e}")
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
                        if triangular_simulate:
                            msg = (f"🚀 *Oportunidade Triangular (Simulada)*\n\n"
                                   f"`{pares_fmt}`\n"
                                   f"Lucro Previsto: `{profit_est_pct:.3%}` (~`{profit_est_abs:.4f} USDT`)\n")
                            registrar_ciclo_triangular(pares_fmt, float(profit_est_pct), float(profit_est_abs), "SIMULATE", "OK")
                            await send_telegram_message(msg)
                        else:
                            # Aqui vai a lógica de execução real
                            msg = (f"✅ *Arbitragem Triangular (Finalizada)*\n\n"
                                   f"`{pares_fmt}`\n"
                                   f"Lucro Real: `{profit_est_pct:.3%}` (~`{profit_est_abs:.4f} USDT`)\n"
                                   f"Saldos: `[saldos aqui]`")
                            registrar_ciclo_triangular(pares_fmt, float(profit_est_pct), float(profit_est_abs), "LIVE", "OK")
                            await send_telegram_message(msg)
                except Exception:
                    pass
        except Exception as e_loop:
            print(f"[ERRO-LOOP-TRIANGULAR] {e_loop}")
            await send_telegram_message(f"⚠️ *Erro no Bot Triangular:* `{e_loop}`")
        await asyncio.sleep(20)

# ==============================================================================
# 4. MÓDULO DE ARBITRAGEM DE FUTUROS (MULTI-EXCHANGE)
# ==============================================================================
active_futures_exchanges = {}
# Corrigido: a contagem agora é feita globalmente
futures_monitored_pairs_count = len(os.getenv("FUTURES_TARGET_PAIRS", "").split(',')) if os.getenv("FUTURES_TARGET_PAIRS", "") else 0

FUTURES_TARGET_PAIRS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT', 
    'DOGE/USDT:USDT', 'LINK/USDT:USDT', 'PEPE/USDT:USDT', 'WLD/USDT:USDT',
    'ADA/USDT:USDT', 'AVAX/USDT:USDT', 'LTC/USDT:USDT', 'DOT/USDT:USDT',
    'BNB/USDT:USDT', 'NEAR/USDT:USDT', 'SUI/USDT:USDT', 'SHIB/USDT:USDT',
    'TRX/USDT:USDT', 'AR/USDT:USDT', 'ICP/USDT:USDT', 'MATIC/USDT:USDT'
]

async def initialize_futures_exchanges():
    global active_futures_exchanges
    if not ccxt: return
    print("[INFO] Inicializando exchanges para o MODO FUTUROS...")
    for name, creds in API_KEYS_FUTURES.items():
        if not creds or not creds.get('apiKey'): continue
        instance = None
        try:
            exchange_class = getattr(ccxt, name)
            instance = exchange_class({**creds, 'options': {'defaultType': 'swap'}})
            await instance.load_markets()
            active_futures_exchanges[name] = instance
            print(f"[INFO-FUTUROS] Exchange '{name}' carregada.")
        except Exception as e:
            print(f"[ERRO-FUTUROS] Falha ao instanciar '{name}': {e}")
            await send_telegram_message(f"❌ *Erro de Conexão:* Falha ao conectar em `{name}`: `{e}`")
            if instance: await instance.close()

async def find_futures_opportunities():
    tasks = {name: ex.fetch_tickers(FUTURES_TARGET_PAIRS) for name, ex in active_futures_exchanges.items()}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    prices_by_symbol = {}
    for (name, _), res in zip(tasks.items(), results):
        if isinstance(res, Exception): continue
        for symbol, ticker in res.items():
            if symbol not in prices_by_symbol: prices_by_symbol[symbol] = []
            if ticker.get('bid') and ticker.get('ask'):
                prices_by_symbol[symbol].append({
                    'exchange': name,
                    'bid': Decimal(ticker['bid']),
                    'ask': Decimal(ticker['ask'])
                })
    opportunities = []
    for symbol, prices in prices_by_symbol.items():
        if len(prices) < 2: continue
        best_ask = min(prices, key=lambda x: x['ask'])
        best_bid = max(prices, key=lambda x: x['bid'])
        if best_ask['exchange'] != best_bid['exchange']:
            profit_pct = ((best_bid['bid'] - best_ask['ask']) / best_ask['ask']) * 100
            if profit_pct > futures_min_profit_threshold:
                opportunities.append({
                    'symbol': symbol,
                    'buy_exchange': best_ask['exchange'],
                    'buy_price': best_ask['ask'],
                    'sell_exchange': best_bid['exchange'],
                    'sell_price': best_bid['bid'],
                    'profit_percent': profit_pct
                })
    return sorted(opportunities, key=lambda x: x['profit_percent'], reverse=True)

async def fechar_posicao_em_caso_de_falha(exchange_name, symbol, side, amount, error_reason):
    msg = (f"🚨 *ALERTA VERMELHO: FALHA NA ARBITRAGEM*\n\n"
           f"Não foi possível fechar a posição em `{exchange_name}`\n"
           f"Par: `{symbol}`\n"
           f"Motivo: `{error_reason}`\n\n"
           f"Tente o comando: `/fechar_posicao {exchange_name} {symbol} {side} {amount}`")
    await send_telegram_message(msg)

async def loop_bot_futures():
    global futures_monitored_pairs_count, active_futures_exchanges, futures_trades_executed
    if not ccxt:
        print("[AVISO] Bot de Futuros desativado.")
        return
    await initialize_futures_exchanges()
    if not active_futures_exchanges:
        msg = "⚠️ *Bot de Futuros não iniciado:* Nenhuma chave de API válida encontrada."
        print(msg)
        await send_telegram_message(msg)
        return
    await send_telegram_message(f"✅ *Bot de Arbitragem de Futuros iniciado.* Exchanges ativas: `{', '.join(active_futures_exchanges.keys())}`")
    
    # Corrigido: atualiza a contagem de pares monitorados aqui
    futures_monitored_pairs_count = len(FUTURES_TARGET_PAIRS)
    
    while True:
        if not futures_running:
            await asyncio.sleep(30)
            continue
        
        if futures_trade_limit > 0 and futures_trades_executed >= futures_trade_limit:
            print("[INFO] Limite de trades alcançado. Desativando o bot de futuros.")
            futures_running = False
            await send_telegram_message(f"🛑 *Limite de trades alcançado:* O bot de futuros foi desativado automaticamente após {futures_trade_limit} trades.")
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
async def get_futures_leverage(exchange_name, symbol):
    if not ccxt or exchange_name not in active_futures_exchanges: return "N/A"
    ex = active_futures_exchanges[exchange_name]
    try:
        positions = await ex.fetch_positions([symbol])
        if positions and len(positions) > 0:
            for p in positions:
                if p['symbol'] == symbol and p['leverage'] is not None:
                    return p['leverage']
        return "N/A"
    except Exception as e:
        return f"Erro: {e}"

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Olá! O CryptoAlerts bot está online e rodando em segundo plano. Use /ajuda para ver os comandos.")

async def ajuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ajuda_text = (
        "🤖 *Comandos do Bot:*\n\n"
        "`/status` - Vê o status atual dos bots e configurações.\n"
        "`/saldos` - Vê o saldo de todas as exchanges conectadas.\n"
        "`/setlucro <triangular> <futuros>` - Define o lucro mínimo em decimal (ex: `0.003 0.5`).\n"
        "`/setvolume <triangular> <futuros>` - Define o volume. Use `%` para porcentagem do saldo (ex: `100 2%`).\n"
        "`/setlimite <num_trades>` - Define o número máximo de trades para o bot de futuros (0 para ilimitado).\n"
        "`/setalavancagem <ex> <par> <val>` - Ajusta a alavancagem de um par (ex: `okx BTC/USDT:USDT 20`).\n"
        "`/ligar <bot>` - Liga um bot (`triangular` ou `futuros`).\n"
        "`/desligar <bot>` - Desliga um bot.\n"
        "`/fechar_posicao <ex> <par> <lado> <qtde>` - Tenta fechar uma posição de futuros manualmente.\n"
    )
    await update.message.reply_text(ajuda_text, parse_mode="Markdown")

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    futures_leverage_text = ""
    futures_leverages = {}
    if active_futures_exchanges:
        tasks = {name: get_futures_leverage(name, 'BTC/USDT:USDT') for name in active_futures_exchanges.keys()}
        leverage_results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for (name, _), res in zip(tasks.items(), leverage_results):
            futures_leverages[name] = res

    for ex_name, lev_val in futures_leverages.items():
        futures_leverage_text += f" | {ex_name.upper()}: `{lev_val}x`"
    
    def get_volume_text(is_triangular):
        amount = triangular_trade_amount if is_triangular else futures_trade_amount
        is_perc = triangular_trade_amount_is_percentage if is_triangular else futures_trade_amount_is_percentage
        if is_perc:
            return f"`{amount}%` da banca (margem)"
        return f"`{amount}` USDT"

    status_text = (
        "📊 *Status Geral dos Bots*\n\n"
        f"**Arbitragem Triangular (OKX Spot):**\n"
        f"Status: `{'ATIVO' if triangular_running else 'DESATIVADO'}`\n"
        f"Modo: `{'SIMULAÇÃO' if triangular_simulate else 'REAL'}`\n"
        f"Lucro Mínimo: `{triangular_min_profit_threshold:.3%}`\n"
        f"Volume de Trade: {get_volume_text(True)}\n"
        f"Ciclos Monitorados: `{triangular_monitored_cycles_count}`\n"
        f"Lucro Total (Simulado): `{triangular_lucro_total_usdt:.4f} USDT`\n\n"
        f"**Arbitragem de Futuros (Multi-Exchange):**\n"
        f"Status: `{'ATIVO' if futures_running else 'DESATIVADO'}`\n"
        f"Modo: `{'SIMULAÇÃO' if futures_dry_run else 'REAL'}`\n"
        f"Lucro Mínimo: `{futures_min_profit_threshold:.2f}%`\n"
        f"Volume de Trade: {get_volume_text(False)}\n"
        f"Pares Monitorados: `{futures_monitored_pairs_count}`\n"
        f"Trades Executados: `{futures_trades_executed}`\n"
        f"Limite de Trades: `{'Ilimitado' if futures_trade_limit == 0 else futures_trade_limit}`\n"
        f"Exchanges Ativas: `{', '.join(active_futures_exchanges.keys())}`\n"
        f"Alavancagem (BTC/USDT):{futures_leverage_text}"
    )
    await update.message.reply_text(status_text, parse_mode="Markdown")

async def saldos_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ccxt:
        await update.message.reply_text("Erro: Módulo 'ccxt' não disponível.")
        return
    
    if not active_futures_exchanges:
        await update.message.reply_text("Nenhuma exchange de futuros está conectada. Verifique suas chaves de API.")
        return
    
    balances_text = "💰 *Saldos Atuais (USDT)*\n\n"
    for name, ex in active_futures_exchanges.items():
        try:
            balance = await ex.fetch_balance()
            total_usdt = Decimal(balance.get('total', {}).get('USDT', 0))
            free_usdt = Decimal(balance.get('free', {}).get('USDT', 0))
            
            balances_text += (f"*{name.upper()}*\n"
                              f"  `Total: {total_usdt:.2f} USDT`\n"
                              f"  `Disponível: {free_usdt:.2f} USDT`\n\n")
        except Exception as e:
            balances_text += f"*{name.upper()}*: Erro ao carregar saldo. `{e}`\n\n"
            
    await update.message.reply_text(balances_text, parse_mode="Markdown")

async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_min_profit_threshold, futures_min_profit_threshold
    try:
        args = context.args
        if len(args) != 2:
            await update.message.reply_text("Uso: `/setlucro <triangular> <futuros>`\n(Ex: `0.003 0.5`)", parse_mode="Markdown")
            return
        triangular_profit = Decimal(args[0])
        futures_profit = Decimal(args[1])
        triangular_min_profit_threshold = triangular_profit
        futures_min_profit_threshold = futures_profit
        await update.message.reply_text(f"Lucro mínimo atualizado: Triangular `{triangular_profit:.3%}` | Futuros `{futures_profit:.2f}%`")
    except (ValueError, IndexError):
        await update.message.reply_text("Valores inválidos. Use `/setlucro <triangular> <futuros>` com números.", parse_mode="Markdown")

async def setvolume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_trade_amount, triangular_trade_amount_is_percentage
    global futures_trade_amount, futures_trade_amount_is_percentage
    try:
        args = context.args
        if len(args) != 2:
            await update.message.reply_text("Uso: `/setvolume <triangular> <futuros>`\n(Ex: `50 100` ou `2% 3%`)", parse_mode="Markdown")
            return

        def parse_volume_arg(arg_str):
            is_perc = False
            if arg_str.endswith('%'):
                is_perc = True
                arg_str = arg_str[:-1]
            return Decimal(arg_str), is_perc

        tri_vol, tri_is_perc = parse_volume_arg(args[0])
        fut_vol, fut_is_perc = parse_volume_arg(args[1])

        triangular_trade_amount = tri_vol
        triangular_trade_amount_is_percentage = tri_is_perc
        futures_trade_amount = fut_vol
        futures_trade_amount_is_percentage = fut_is_perc
        
        tri_text = f"`{tri_vol}%` do saldo" if tri_is_perc else f"`{tri_vol}` USDT"
        fut_text = f"`{fut_vol}%` da banca" if fut_is_perc else f"`{fut_vol}` USDT"

        await update.message.reply_text(f"Volume de trade atualizado:\nTriangular: {tri_text}\nFuturos: {fut_text}")
    except (ValueError, IndexError):
        await update.message.reply_text("Valores inválidos. Use `/setvolume <triangular> <futuros>`.", parse_mode="Markdown")

async def setlimite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global futures_trade_limit, futures_trades_executed
    try:
        if not context.args:
            await update.message.reply_text(f"Limite atual: `{'Ilimitado' if futures_trade_limit == 0 else futures_trade_limit}`. Trades executados: `{futures_trades_executed}`\n\nUso: `/setlimite <número>` (0 para ilimitado).", parse_mode="Markdown")
            return
        
        limit = int(context.args[0])
        if limit < 0:
            await update.message.reply_text("O limite deve ser um número inteiro positivo ou zero.", parse_mode="Markdown")
            return
            
        futures_trade_limit = limit
        futures_trades_executed = 0
        
        limit_text = f"`{futures_trade_limit}` trades" if futures_trade_limit > 0 else "Ilimitado"
        await update.message.reply_text(f"Limite de trades para o bot de futuros definido para: {limit_text}. O contador foi resetado.", parse_mode="Markdown")
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
        
        exchange_name, symbol, leverage_str = args
        leverage = int(leverage_str)
        
        if exchange_name.lower() not in active_futures_exchanges:
            await update.message.reply_text(f"Exchange `{exchange_name}` não está conectada ou é inválida.")
            return

        exchange = active_futures_exchanges[exchange_name.lower()]
        
        await update.message.reply_text(f"Tentando definir alavancagem de `{symbol}` para `{leverage}x` em `{exchange_name}`...")
        
        try:
            await exchange.set_leverage(leverage, symbol, params={'mgnMode': 'cross'})
            await update.message.reply_text(f"✅ Alavancagem de `{symbol}` em `{exchange_name}` definida para `{leverage}x` com sucesso!")
        except Exception as e:
            await update.message.reply_text(f"❌ Falha ao definir alavancagem: `{e}`")
            
    except (ValueError, IndexError):
        await update.message.reply_text("Valores inválidos. Verifique se a alavancagem é um número inteiro.", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"Erro ao processar o comando: `{e}`")

async def fechar_posicao_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ccxt:
        await update.message.reply_text("Erro: O módulo 'ccxt' não está disponível.")
        return
    try:
        args = context.args
        if len(args) != 4:
            await update.message.reply_text("Uso: `/fechar_posicao <ex> <par> <lado> <qtde>`\nEx: `/fechar_posicao okx BTC/USDT:USDT sell 0.001`", parse_mode="Markdown")
            return
        exchange_name, symbol, side, amount = args
        await update.message.reply_text(f"Comando recebido: tentando fechar posição em `{exchange_name}` para `{symbol}`...")
        
        try:
            exchange_class = getattr(ccxt, exchange_name)
            creds = API_KEYS_FUTURES.get(exchange_name)
            if not creds: raise ValueError(f"Credenciais para {exchange_name} não encontradas.")
            exchange = exchange_class({**creds, 'options': {'defaultType': 'swap'}})
            
            parsed_symbol = exchange.parse_symbol(symbol)
            opposite_side = 'sell' if side.lower() == 'buy' else 'buy'
            
            order = await exchange.create_order(
                symbol=parsed_symbol,
                type='market',
                side=opposite_side,
                amount=float(amount)
            )
            await exchange.close()
            await update.message.reply_text(f"✅ Ordem de fechamento enviada para `{exchange_name}`: `{order['id']}`.")
        except Exception as e:
            await update.message.reply_text(f"❌ Falha ao fechar posição em `{exchange_name}`: `{e}`")
    except Exception as e:
        await update.message.reply_text(f"Erro ao processar o comando: {e}")

async def ligar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_running, futures_running
    try:
        bot_name = context.args[0].lower()
        if bot_name == 'triangular':
            triangular_running = True
            await update.message.reply_text("Bot triangular ativado.")
        elif bot_name == 'futuros':
            futures_running = True
            await update.message.reply_text("Bot de futuros ativado.")
        else:
            await update.message.reply_text("Bot inválido. Use 'triangular' ou 'futuros'.")
    except IndexError:
        await update.message.reply_text("Uso: `/ligar <bot>`", parse_mode="Markdown")

async def desligar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global triangular_running, futures_running
    try:
        bot_name = context.args[0].lower()
        if bot_name == 'triangular':
            triangular_running = False
            await update.message.reply_text("Bot triangular desativado.")
        elif bot_name == 'futuros':
            futures_running = False
            await update.message.reply_text("Bot de futuros desativado.")
        else:
            await update.message.reply_text("Bot inválido. Use 'triangular' ou 'futuros'.")
    except IndexError:
        await update.message.reply_text("Uso: `/desligar <bot>`", parse_mode="Markdown")

async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Comando desconhecido. Use `/ajuda` para ver os comandos válidos.")


async def main():
    """Roda o bot e os loops de arbitragem no mesmo processo."""
    print("[INFO] Iniciando bot...")
    
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
    application.add_handler(CommandHandler("fechar_posicao", fechar_posicao_command))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    init_triangular_db()
    asyncio.create_task(loop_bot_triangular())
    
    if ccxt:
        asyncio.create_task(loop_bot_futures())
    
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        await send_telegram_message("✅ *Bot iniciado e conectado ao Telegram!*")

    print("[INFO] Bot do Telegram rodando...")
    await application.run_polling()
    
    
async def graceful_shutdown(loop, application, futures_exchanges):
    print("Sinal de término recebido. Iniciando encerramento seguro...")
    
    if application:
        await application.shutdown()

    if futures_exchanges:
        for ex in futures_exchanges.values():
            await ex.close()
    
    loop.stop()


def setup_signal_handler(loop, application, futures_exchanges):
    try:
        loop.add_signal_handler(signal.SIGTERM, lambda: loop.create_task(graceful_shutdown(loop, application, futures_exchanges)))
    except NotImplementedError:
        print("Aviso: Falha ao adicionar handler de sinal SIGTERM. O encerramento pode não ser seguro.")


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
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
    application.add_handler(CommandHandler("fechar_posicao", fechar_posicao_command))
    application.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    init_triangular_db()
    loop.create_task(loop_bot_triangular())
    
    if ccxt:
        loop.create_task(loop_bot_futures())
    
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        loop.create_task(send_telegram_message("✅ *Bot iniciado e conectado ao Telegram!*"))

    print("[INFO] Bot do Telegram rodando...")
    setup_signal_handler(loop, application, active_futures_exchanges)
    
    try:
        loop.run_until_complete(application.run_polling())
        loop.run_forever()
    finally:
        loop.close()
