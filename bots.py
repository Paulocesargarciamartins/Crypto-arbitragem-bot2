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
from flask import Flask, request
from concurrent.futures import ThreadPoolExecutor

# Tenta importar o ccxt, necessário para o bot de futuros
try:
    import ccxt.async_support as ccxt
except ImportError:
    print("[AVISO] Biblioteca 'ccxt' não encontrada. A função de arbitragem de futuros será desativada.")
    ccxt = None

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
    'bybit': {'apiKey': os.getenv('BYBIT_API_KEY'), 'secret': os.getenv('BYBIT_API_SECRET')},
    'kucoin': {'apiKey': os.getenv('KUCOIN_API_KEY'), 'secret': os.getenv('KUCOIN_API_SECRET'), 'password': os.getenv('KUCOIN_API_PASSPHRASE')},
    'gateio': {'apiKey': os.getenv('GATEIO_API_KEY'), 'secret': os.getenv('GATEIO_API_SECRET')},
    'mexc': {'apiKey': os.getenv('MEXC_API_KEY'), 'secret': os.getenv('MEXC_API_SECRET')},
    'bitget': {'apiKey': os.getenv('BITGET_API_KEY'), 'secret': os.getenv('BITGET_API_SECRET'), 'password': os.getenv('BITGET_API_PASSPHRASE')},
}

# --- Inicialização do Flask ---
app = Flask(__name__)
executor = ThreadPoolExecutor(max_workers=5)

# ==============================================================================
# 2. FUNÇÕES AUXILIARES GLOBAIS
# ==============================================================================
def send_telegram_message(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Erro ao enviar mensagem no Telegram: {e}")

def okx_server_iso_time():
    try:
        r = requests.get("https://www.okx.com/api/v5/public/time", timeout=5)
        r.raise_for_status()
        ts_ms = int(r.json()["data"][0]["ts"])
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except Exception:
        return datetime.utcnow().replace(tzinfo=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

def generate_okx_signature(timestamp, method, request_path, body=""):
    message = f"{timestamp}{method}{request_path}{body}"
    mac = hmac.new(OKX_API_SECRET.encode("utf-8"), message.encode("utf-8"), digestmod="sha256")
    return base64.b64encode(mac.digest()).decode()

def get_okx_headers(method, path, body_dict=None):
    ts = okx_server_iso_time()
    body = json.dumps(body_dict) if body_dict else ""
    return {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": generate_okx_signature(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": OKX_API_PASSPHRASE,
        "Content-Type": "application/json",
    }

# ==============================================================================
# 3. MÓDULO DE ARBITRAGEM TRIANGULAR (OKX SPOT)
# ==============================================================================
TRIANGULAR_TRADE_AMOUNT_USDT = Decimal(os.getenv("TRADE_AMOUNT_USDT", "10"))
TRIANGULAR_MIN_PROFIT_THRESHOLD = Decimal(os.getenv("MIN_PROFIT_THRESHOLD", "0.002"))
TRIANGULAR_SIMULATE = os.getenv("TRIANGULAR_SIMULATE", "true").lower() in ["1", "true", "yes"]
TRIANGULAR_DB_FILE = "historico_triangular.db"
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

def simulate_triangular_cycle(cycle, tickers):
    amt = TRIANGULAR_TRADE_AMOUNT_USDT
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
    profit_abs = final_usdt - TRIANGULAR_TRADE_AMOUNT_USDT
    profit_pct = profit_abs / TRIANGULAR_TRADE_AMOUNT_USDT if TRIANGULAR_TRADE_AMOUNT_USDT > 0 else 0
    return profit_pct, profit_abs

def loop_bot_triangular():
    global triangular_monitored_cycles_count
    print("[INFO] Bot de Arbitragem Triangular (OKX Spot) iniciado.")
    
    try:
        print("[INFO-TRIANGULAR] Buscando todos os instrumentos da OKX para construir ciclos dinâmicos...")
        all_instruments = get_all_okx_spot_instruments()
        dynamic_cycles = build_dynamic_cycles(all_instruments)
        triangular_monitored_cycles_count = len(dynamic_cycles)
        print(f"[INFO-TRIANGULAR] {triangular_monitored_cycles_count} ciclos de arbitragem foram construídos dinamicamente.")
        if triangular_monitored_cycles_count == 0:
            send_telegram_message("⚠️ *Aviso Triangular:* Nenhum ciclo de arbitragem pôde ser construído.")
    except Exception as e:
        print(f"[ERRO-CRÍTICO-TRIANGULAR] Falha ao construir ciclos dinâmicos: {e}")
        send_telegram_message(f"❌ *Erro Crítico Triangular:* Falha ao construir ciclos. Erro: `{e}`")
        return

    while True:
        try:
            all_inst_ids_needed = list({instId for cycle in dynamic_cycles for instId, _ in cycle})
            all_tickers = get_okx_spot_tickers(all_inst_ids_needed)
            
            for cycle in dynamic_cycles:
                try:
                    profit_est_pct, profit_est_abs = simulate_triangular_cycle(cycle, all_tickers)
                    
                    if profit_est_pct > TRIANGULAR_MIN_PROFIT_THRESHOLD:
                        pares_fmt = " → ".join([p for p, a in cycle])
                        msg = (f"🚀 *Oportunidade Triangular (OKX Spot)*\n\n"
                               f"`{pares_fmt}`\n"
                               f"Lucro Previsto: `{profit_est_pct:.3%}` (~`{profit_est_abs:.4f} USDT`)\n"
                               f"Modo: `{'SIMULAÇÃO' if TRIANGULAR_SIMULATE else 'REAL'}`")
                        send_telegram_message(msg)
                        
                        if TRIANGULAR_SIMULATE:
                            registrar_ciclo_triangular(pares_fmt, float(profit_est_pct), float(profit_est_abs), "SIMULATE", "OK")
                        else:
                            registrar_ciclo_triangular(pares_fmt, float(profit_est_pct), float(profit_est_abs), "LIVE_EXECUTION_SKIPPED", "OK")
                except Exception:
                    pass
        
        except Exception as e_loop:
            print(f"[ERRO-LOOP-TRIANGULAR] {e_loop}")
            send_telegram_message(f"⚠️ *Erro no Bot Triangular:* `{e_loop}`")
        
        time.sleep(20)

# ==============================================================================
# 4. MÓDULO DE ARBITRAGEM DE FUTUROS (MULTI-EXCHANGE)
# ==============================================================================
FUTURES_DRY_RUN = os.getenv("FUTURES_DRY_RUN", "true").lower() in ["1", "true", "yes"]
FUTURES_MIN_PROFIT_THRESHOLD = Decimal("0.3")
active_futures_exchanges = {}
futures_monitored_pairs_count = 0

FUTURES_TARGET_PAIRS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT', 
    'DOGE/USDT:USDT', 'LINK/USDT:USDT', 'PEPE/USDT:USDT', 'WLD/USDT:USDT'
]

async def initialize_futures_exchanges():
    global active_futures_exchanges
    if not ccxt: return
    
    print("[INFO] Inicializando exchanges para o MODO FUTUROS...")
    for name, creds in API_KEYS_FUTURES.items():
        if not creds or not creds.get('apiKey'): continue
        try:
            exchange_class = getattr(ccxt, name)
            instance = exchange_class({**creds, 'options': {'defaultType': 'swap'}})
            await instance.load_markets()
            active_futures_exchanges[name] = instance
            print(f"[INFO-FUTUROS] Exchange '{name}' carregada.")
        except Exception as e:
            print(f"[ERRO-FUTUROS] Falha ao instanciar '{name}': {e}")

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
            if profit_pct > FUTURES_MIN_PROFIT_THRESHOLD:
                opportunities.append({
                    'symbol': symbol,
                    'buy_exchange': best_ask['exchange'],
                    'buy_price': best_ask['ask'],
                    'sell_exchange': best_bid['exchange'],
                    'sell_price': best_bid['bid'],
                    'profit_percent': profit_pct
                })
    return sorted(opportunities, key=lambda x: x['profit_percent'], reverse=True)

async def loop_bot_futures():
    global futures_monitored_pairs_count
    if not ccxt:
        print("[AVISO] Bot de Futuros desativado pois a biblioteca 'ccxt' não está instalada.")
        return

    await initialize_futures_exchanges()
    if not active_futures_exchanges:
        msg = "⚠️ *Bot de Futuros não iniciado:* Nenhuma chave de API válida encontrada."
        print(msg)
        send_telegram_message(msg)
        return
        
    send_telegram_message(f"✅ *Bot de Arbitragem de Futuros iniciado.* Exchanges ativas: `{', '.join(active_futures_exchanges.keys())}`")

    while True:
        try:
            futures_monitored_pairs_count = len(FUTURES_TARGET_PAIRS)
            opportunities = await find_futures_opportunities()
            
            if opportunities:
                opp = opportunities[0]
                msg = (f"💸 *Oportunidade de Futuros Detectada!*\n\n"
                       f"Par: `{opp['symbol']}`\n"
                       f"Comprar em: `{opp['buy_exchange'].upper()}` a `{opp['buy_price']}`\n"
                       f"Vender em: `{opp['sell_exchange'].upper()}` a `{opp['sell_price']}`\n"
                       f"Lucro Potencial: *`{opp['profit_percent']:.3f}%`*\n"
                       f"Modo: `{'SIMULAÇÃO' if FUTURES_DRY_RUN else 'REAL'}`")
                send_telegram_message(msg)
        
        except Exception as e:
            print(f"[ERRO-LOOP-FUTUROS] {e}")
            send_telegram_message(f"⚠️ *Erro no Bot de Futuros:* `{e}`")
            
        await asyncio.sleep(90)

# ==============================================================================
# 5. CONTROLE VIA TELEGRAM (WEBHOOK FLASK)
# ==============================================================================
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    data = request.get_json(force=True)
    msg_text = data.get("message", {}).get("text", "").strip().lower()
    
    if msg_text == "/ping":
        send_telegram_message("Pong! 🏓 O servidor web está respondendo.")
    elif msg_text == "/ajuda":
        send_telegram_message("🤖 *Bot Online!* Comandos de status e controle estão sendo reavaliados para a nova arquitetura.")
    else:
        send_telegram_message(f"Recebido: `{msg_text}`. O bot está processando em segundo plano.")

    return "OK", 200

# ==============================================================================
# 6. LÓGICA DE INICIALIZAÇÃO SEPARADA
# ==============================================================================
def run_futures_bot_in_loop():
    if ccxt:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(loop_bot_futures())
        loop.close()

def run_all_bots():
    """Esta função inicia os loops de ambos os bots de arbitragem."""
    print("[INFO] Iniciando processo dos bots de arbitragem...")
    send_telegram_message("🤖 *Processo dos bots de arbitragem iniciado.* Analisando mercados...")
    
    init_triangular_db()
    
    thread_triangular = threading.Thread(target=loop_bot_triangular, daemon=True)
    thread_triangular.start()
    
    if ccxt:
        thread_futures = threading.Thread(target=run_futures_bot_in_loop, daemon=True)
        thread_futures.start()
    
    thread_triangular.join()
    if ccxt:
        thread_futures.join()

# O código abaixo verifica qual tipo de worker o Heroku está tentando iniciar.
if __name__ == "__main__":
    # O Gunicorn passa o nome do módulo:app como argumento.
    # Se esse padrão for encontrado, estamos no processo 'web'.
    is_web_process = len(sys.argv) > 1 and sys.argv[1].endswith(':app')
    
    if is_web_process:
        # Este é o processo WEB. O Gunicorn vai cuidar de iniciar o 'app' Flask.
        # Não precisamos fazer nada aqui, apenas logar.
        print("[INFO] Processo iniciado em modo WEB (Gunicorn).")
        send_telegram_message("🌐 *Servidor Web iniciado e ouvindo o Telegram.*")
    else:
        # Este é o processo BOT.
        run_all_bots()
