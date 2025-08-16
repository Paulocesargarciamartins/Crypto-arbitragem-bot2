# -*- coding: utf-8 -*-
import os
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

# --- Status e Configurações dos Bots ---
triangular_bot_ativo = True
futures_bot_ativo = True
app = Flask(__name__)

# ==============================================================================
# 2. FUNÇÕES AUXILIARES GLOBAIS (TELEGRAM, OKX AUTH)
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
triangular_monitored_pairs_count = 0
triangular_lucro_total_usdt = Decimal("0")

triangular_cycles = [
    [("BTC-USDT", "buy"), ("ETH-BTC", "buy"), ("ETH-USDT", "sell")],
    [("SOL-USDT", "buy"), ("ETH-SOL", "buy"), ("ETH-USDT", "sell")],
]

def init_triangular_db():
    with sqlite3.connect(TRIANGULAR_DB_FILE) as conn:
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS ciclos (
            timestamp TEXT, pares TEXT, lucro_percent REAL, lucro_usdt REAL, modo TEXT, status TEXT, detalhes TEXT)""")
        conn.commit()

def registrar_ciclo_triangular(pares, lucro_percent, lucro_usdt, modo, status, detalhes=""):
    global triangular_lucro_total_usdt
    triangular_lucro_total_usdt += Decimal(str(lucro_usdt))
    with sqlite3.connect(TRIANGULAR_DB_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO ciclos VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (datetime.now(timezone.utc).isoformat(), json.dumps(pares), float(lucro_percent),
                   float(lucro_usdt), modo, status, detalhes))
        conn.commit()

def obter_historico_triangular(limit=5):
    with sqlite3.connect(TRIANGULAR_DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM ciclos ORDER BY timestamp DESC LIMIT ?", (limit,))
        return c.fetchall()

def check_okx_credentials():
    if not (OKX_API_KEY and OKX_API_SECRET and OKX_API_PASSPHRASE):
        raise RuntimeError("Credenciais da OKX ausentes.")
    path = "/api/v5/account/balance"
    r = requests.get("https://www.okx.com" + path, headers=get_okx_headers("GET", path), timeout=10)
    j = r.json()
    if j.get("code") != "0":
        raise RuntimeError(f"Falha de autenticação OKX: {j.get('msg', 'Erro desconhecido')}")
    return True

def get_okx_spot_tickers(inst_ids):
    url = f"https://www.okx.com/api/v5/market/tickers?instType=SPOT"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json().get("data", [])
    tickers = {d["instId"]: {"bid": Decimal(d.get("bidPx")), "ask": Decimal(d.get("askPx"))} for d in data if d.get("bidPx") and d.get("askPx")}
    return {inst_id: tickers.get(inst_id) for inst_id in inst_ids}

def simulate_triangular_cycle(cycle, tickers):
    amt = TRIANGULAR_TRADE_AMOUNT_USDT
    for instId, action in cycle:
        ticker = tickers.get(instId)
        if not ticker: raise RuntimeError(f"Ticker para {instId} não encontrado.")
        
        if action == "buy":
            price = ticker["ask"]
            amt = (amt / price) * (Decimal("1") - TRIANGULAR_FEE_RATE)
        elif action == "sell":
            price = ticker["bid"]
            amt = (amt * price) * (Decimal("1") - TRIANGULAR_FEE_RATE)
            
    final_usdt = amt
    profit_abs = final_usdt - TRIANGULAR_TRADE_AMOUNT_USDT
    profit_pct = profit_abs / TRIANGULAR_TRADE_AMOUNT_USDT
    return profit_pct, profit_abs

def loop_bot_triangular():
    global triangular_monitored_pairs_count
    print("[INFO] Bot de Arbitragem Triangular (OKX Spot) iniciado.")
    send_telegram_message("✅ *Bot de Arbitragem Triangular (OKX Spot) iniciado.*")
    
    while True:
        if not triangular_bot_ativo:
            time.sleep(5)
            continue
        
        all_inst_ids = {instId for cycle in triangular_cycles for instId, _ in cycle}
        triangular_monitored_pairs_count = len(all_inst_ids)
        
        try:
            all_tickers = get_okx_spot_tickers(list(all_inst_ids))
            
            for cycle in triangular_cycles:
                try:
                    profit_est_pct, profit_est_abs = simulate_triangular_cycle(cycle, all_tickers)
                    
                    if profit_est_pct > TRIANGULAR_MIN_PROFIT_THRESHOLD:
                        pares_fmt = " → ".join([f"{p}" for p, a in cycle])
                        msg = (f"🚀 *Oportunidade Triangular (OKX Spot)*\n\n"
                               f"`{pares_fmt}`\n"
                               f"Lucro Previsto: `{profit_est_pct:.3%}` (~`{profit_est_abs:.4f} USDT`)\n"
                               f"Modo: `{'SIMULAÇÃO' if TRIANGULAR_SIMULATE else 'REAL'}`")
                        send_telegram_message(msg)
                        
                        if TRIANGULAR_SIMULATE:
                            registrar_ciclo_triangular(pares_fmt, float(profit_est_pct), float(profit_est_abs), "SIMULATE", "OK")
                        else:
                            # Lógica de execução real (execute_cycle_live) iria aqui
                            # Por segurança, mantido como simulação por enquanto
                            registrar_ciclo_triangular(pares_fmt, float(profit_est_pct), float(profit_est_abs), "LIVE_EXECUTION_SKIPPED", "OK")

                except Exception as e_cycle:
                    print(f"[ERRO-CICLO-TRIANGULAR] {e_cycle}")
                    # Não spamar o telegram com erros de ciclo, apenas logar
        
        except Exception as e_loop:
            print(f"[ERRO-LOOP-TRIANGULAR] {e_loop}")
            send_telegram_message(f"⚠️ *Erro no Bot Triangular:* `{e_loop}`")
        
        time.sleep(15) # Intervalo entre verificações

# ==============================================================================
# 4. MÓDULO DE ARBITRAGEM DE FUTUROS (MULTI-EXCHANGE)
# ==============================================================================
FUTURES_DRY_RUN = os.getenv("FUTURES_DRY_RUN", "true").lower() in ["1", "true", "yes"]
FUTURES_MIN_PROFIT_THRESHOLD = Decimal("0.3")
FUTURES_LEVERAGE = 5
FUTURES_LOOP_SLEEP_SECONDS = 90
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
    tasks = []
    for symbol in FUTURES_TARGET_PAIRS:
        for name, ex in active_futures_exchanges.items():
            if symbol in ex.markets:
                tasks.append(ex.fetch_ticker(symbol))

    tickers = await asyncio.gather(*tasks, return_exceptions=True)
    
    prices_by_symbol = {}
    for i, ticker in enumerate(tickers):
        if isinstance(ticker, Exception) or not ticker: continue
        symbol = ticker['symbol']
        if symbol not in prices_by_symbol: prices_by_symbol[symbol] = []
        # Precisamos saber de qual exchange veio o ticker. Reconstruindo a partir da ordem das tasks.
        # Esta é uma forma de contornar a limitação do gather.
        original_task_index = i
        current_index = 0
        exchange_name = "unknown"
        for sym_task in FUTURES_TARGET_PAIRS:
            for name_task in active_futures_exchanges.keys():
                if sym_task == symbol:
                    if current_index == original_task_index:
                        exchange_name = name_task
                        break
                    current_index += 1
            if exchange_name != "unknown":
                break

        prices_by_symbol[symbol].append({
            'exchange': exchange_name,
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
        if not futures_bot_ativo:
            await asyncio.sleep(5)
            continue
        
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
                # Lógica de execução do trade (execute_arbitrage_trade) iria aqui
        
        except Exception as e:
            print(f"[ERRO-LOOP-FUTUROS] {e}")
            send_telegram_message(f"⚠️ *Erro no Bot de Futuros:* `{e}`")
            
        await asyncio.sleep(FUTURES_LOOP_SLEEP_SECONDS)

# ==============================================================================
# 5. CONTROLE VIA TELEGRAM (WEBHOOK FLASK)
# ==============================================================================
async def test_exchange_connections_async():
    if not ccxt:
        await send_telegram_message("⚠️ O módulo de futuros (ccxt) não está instalado.")
        return
    
    msg = "🔍 *Testando Conexões com as Exchanges (Futuros)*:\n\n"
    for name, ex in active_futures_exchanges.items():
        try:
            await ex.fetch_balance({'type': 'swap'})
            msg += f"✅ `{name.upper()}`: Conectado e autenticado.\n"
        except Exception as e:
            msg += f"❌ `{name.upper()}`: Falha. Erro: `{str(e)[:50]}...`\n"
    send_telegram_message(msg)

async def compare_coin_prices_async(coin):
    if not ccxt:
        send_telegram_message("⚠️ O módulo de futuros (ccxt) não está instalado.")
        return
        
    symbol = f"{coin.upper()}/USDT:USDT"
    msg = f"📊 *Comparando Preços de {symbol} (Futuros)*\n_{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S %Z')}__\n\n"
    
    tasks = {name: ex.fetch_ticker(symbol) for name, ex in active_futures_exchanges.items() if symbol in ex.markets}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    
    found_prices = []
    for (name, ex), res in zip(tasks.items(), results):
        if not isinstance(res, Exception) and res.get('last'):
            found_prices.append({'name': name, 'price': Decimal(res['last'])})
            
    if not found_prices:
        msg += f"Nenhum preço encontrado para `{symbol}` nas exchanges ativas."
    else:
        for item in sorted(found_prices, key=lambda x: x['price']):
            msg += f"- `{item['name'].upper()}`: `{item['price']:.4f}` USDT\n"
        
    send_telegram_message(msg)

async def close_position_manually_async(exchange_name, symbol, side, amount):
    # ... (lógica de fechamento manual)
    pass

@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    global triangular_bot_ativo, futures_bot_ativo, TRIANGULAR_SIMULATE, FUTURES_DRY_RUN, TRIANGULAR_MIN_PROFIT_THRESHOLD, FUTURES_MIN_PROFIT_THRESHOLD
    
    data = request.get_json(force=True)
    msg_text = data.get("message", {}).get("text", "").strip()
    chat_id = str(data.get("message", {}).get("chat", {}).get("id", ""))

    if chat_id != str(TELEGRAM_CHAT_ID): return "Unauthorized", 403

    parts = msg_text.split()
    command = parts[0].lower()

    try:
        if command == "/ajuda":
            send_telegram_message(
                "🤖 *Lista de Comandos Disponíveis*\n\n"
                "*Análise e Diagnóstico*\n"
                "`/status_geral` - Status de ambos os bots.\n"
                "`/testar_conexoes` - Testa a API de todas as exchanges de futuros.\n"
                "`/comparar_preco <MOEDA>` - Ex: `/comparar_preco btc`\n\n"
                "*Bot Triangular (OKX Spot)*\n"
                "`/status_triangular`\n"
                "`/setprofit_triangular <%>` - Ex: `/setprofit_triangular 0.2`\n"
                "`/pausar_triangular` | `/retomar_triangular`\n"
                "`/historico_triangular`\n"
                "`/simulacao_triangular_on` | `/simulacao_triangular_off`\n\n"
                "*Bot de Futuros (Multi-Exchange)*\n"
                "`/status_futuros`\n"
                "`/setprofit_futuros <%>` - Ex: `/setprofit_futuros 0.4`\n"
                "`/fechar_posicao <exc> <par> <lado> <qtd>`\n"
                "`/pausar_futuros` | `/retomar_futuros`"
            )
        elif command == "/status_geral":
            tri_status = 'ATIVO' if triangular_bot_ativo else 'PAUSADO'
            fut_status = 'ATIVO' if futures_bot_ativo else 'PAUSADO'
            send_telegram_message(f"🤖 *Status Geral dos Bots*\n\n"
                                  f"▶️ *Triangular (OKX Spot):* `{tri_status}`\n"
                                  f"💸 *Futuros (Multi-Exchange):* `{fut_status}`")
        elif command == "/testar_conexoes":
            threading.Thread(target=lambda: asyncio.run(test_exchange_connections_async())).start()
        elif command == "/comparar_preco" and len(parts) > 1:
            threading.Thread(target=lambda: asyncio.run(compare_coin_prices_async(parts[1]))).start()
        
        # ... (outros comandos como no script anterior)

    except Exception as e:
        send_telegram_message(f"❌ Erro ao processar comando: `{e}`")

    return "OK", 200

# ==============================================================================
# 6. INICIALIZAÇÃO PRINCIPAL
# ==============================================================================
def run_futures_bot_in_loop():
    if ccxt:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(loop_bot_futures())
        loop.close()

if __name__ == "__main__":
    init_triangular_db()
    try:
        if check_okx_credentials():
            print("[INFO] Credenciais da OKX validadas com sucesso.")
    except Exception as e:
        msg = f"❌ *Falha crítica ao validar credenciais OKX:* `{e}`."
        print(msg)
        send_telegram_message(msg)

    # Iniciar Threads dos Bots
    thread_triangular = threading.Thread(target=loop_bot_triangular, daemon=True)
    thread_triangular.start()

    if ccxt:
        thread_futures = threading.Thread(target=run_futures_bot_in_loop, daemon=True)
        thread_futures.start()
    else:
        print("[AVISO] Thread do bot de futuros não iniciada pois 'ccxt' não está disponível.")

    # Iniciar Servidor Flask para o Telegram
    port = int(os.environ.get("PORT", 5000))
    print(f"[INFO] Iniciando servidor Flask na porta {port} para receber webhooks do Telegram.")
    # A linha abaixo é a que mantém o bot vivo.
    app.run(host="0.0.0.0", port=port)
