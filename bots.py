# -*- coding: utf-8 -*-
import os
import sys
import time
import requests
import json
import threading
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, getcontext, ROUND_DOWN
from dotenv import load_dotenv
from flask import Flask, request

# --- Importações Condicionais ---
try:
    import ccxt.async_support as ccxt
except ImportError:
    print("Erro: A biblioteca 'ccxt' não está instalada. O bot de futuros não pode funcionar.")
    sys.exit(1)

# ==============================================================================
# 1. CONFIGURAÇÃO GLOBAL E INICIALIZAÇÃO
# ==============================================================================
load_dotenv()
getcontext().prec = 28
getcontext().rounding = ROUND_DOWN

# --- Chaves e Tokens ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
HEROKU_APP_NAME = os.getenv("HEROKU_APP_NAME", "") # Ex: meu-bot-de-futuros

API_KEYS_FUTURES = {
    'okx': {'apiKey': os.getenv('OKX_API_KEY'), 'secret': os.getenv('OKX_API_SECRET'), 'password': os.getenv('OKX_API_PASSPHRASE')},
    'gateio': {'apiKey': os.getenv('GATEIO_API_KEY'), 'secret': os.getenv('GATEIO_API_SECRET')},
    'mexc': {'apiKey': os.getenv('MEXC_API_KEY'), 'secret': os.getenv('MEXC_API_SECRET')},
    'bitget': {'apiKey': os.getenv('BITGET_API_KEY'), 'secret': os.getenv('BITGET_API_SECRET'), 'password': os.getenv('BITGET_API_PASSPHRASE')},
}

# --- Inicialização do Flask ---
app = Flask(__name__)

# --- Variáveis de estado globais ---
futures_running = True
futures_min_profit_threshold = Decimal(os.getenv("FUTURES_MIN_PROFIT_THRESHOLD", "0.3"))
FUTURES_DRY_RUN = os.getenv("FUTURES_DRY_RUN", "true").lower() in ["1", "true", "yes"]
active_futures_exchanges = {}
futures_monitored_pairs_count = 0

FUTURES_TARGET_PAIRS = [
    'BTC/USDT:USDT', 'ETH/USDT:USDT', 'SOL/USDT:USDT', 'XRP/USDT:USDT',
    'DOGE/USDT:USDT', 'LINK/USDT:USDT', 'PEPE/USDT:USDT', 'WLD/USDT:USDT'
]

# ==============================================================================
# 2. FUNÇÕES AUXILIARES E DE TELEGRAM
# ==============================================================================
def send_telegram_message(text, chat_id=None):
    final_chat_id = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not final_chat_id:
        print("[AVISO] Token ou Chat ID do Telegram não configurado.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": final_chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except requests.exceptions.RequestException as e:
        print(f"Erro ao enviar mensagem no Telegram: {e}")

def set_telegram_webhook():
    if not HEROKU_APP_NAME:
        print("[AVISO] HEROKU_APP_NAME não definido. Não é possível configurar o webhook.")
        return
    webhook_url = f"https://{HEROKU_APP_NAME}.herokuapp.com/{TELEGRAM_TOKEN}"
    set_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={webhook_url}"
    try:
        response = requests.get(set_url)
        response.raise_for_status()
        print(f"Webhook configurado com sucesso para: {webhook_url}")
        print(f"Resposta do Telegram: {response.json()}")
    except requests.exceptions.RequestException as e:
        print(f"Erro ao configurar o webhook: {e}")

# ==============================================================================
# 3. LÓGICA DE ARBITRAGEM DE FUTUROS (ASYNCIO)
# ==============================================================================
async def initialize_futures_exchanges():
    global active_futures_exchanges
    print("[INFO] Inicializando exchanges para o MODO FUTUROS...")
    for name, creds in API_KEYS_FUTURES.items():
        if not creds or not creds.get('apiKey'):
            continue
        instance = None
        try:
            exchange_class = getattr(ccxt, name)
            instance = exchange_class({**creds, 'options': {'defaultType': 'swap'}})
            await instance.load_markets()
            active_futures_exchanges[name] = instance
            print(f"[INFO] Exchange '{name}' carregada.")
        except Exception as e:
            print(f"[ERRO] Falha ao instanciar '{name}': {e}")
            if instance: await instance.close()

async def find_futures_opportunities():
    if not active_futures_exchanges: return []
    tasks = {name: ex.fetch_tickers(FUTURES_TARGET_PAIRS) for name, ex in active_futures_exchanges.items()}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    prices_by_symbol = {}
    for (name, _), res in zip(tasks.items(), results):
        if isinstance(res, Exception): continue
        for symbol, ticker in res.items():
            if symbol not in prices_by_symbol: prices_by_symbol[symbol] = []
            if ticker.get('bid') and ticker.get('ask'):
                prices_by_symbol[symbol].append({'exchange': name, 'bid': Decimal(str(ticker['bid'])), 'ask': Decimal(str(ticker['ask']))})
    opportunities = []
    for symbol, prices in prices_by_symbol.items():
        if len(prices) < 2: continue
        best_ask = min(prices, key=lambda x: x['ask'])
        best_bid = max(prices, key=lambda x: x['bid'])
        if best_ask['exchange'] != best_bid['exchange']:
            profit_pct = ((best_bid['bid'] - best_ask['ask']) / best_ask['ask']) * 100
            if profit_pct > futures_min_profit_threshold:
                opportunities.append({'symbol': symbol, 'buy_exchange': best_ask['exchange'], 'buy_price': best_ask['ask'], 'sell_exchange': best_bid['exchange'], 'sell_price': best_bid['bid'], 'profit_percent': profit_pct})
    return sorted(opportunities, key=lambda x: x['profit_percent'], reverse=True)

async def main_futures_loop():
    global futures_monitored_pairs_count
    await initialize_futures_exchanges()
    if not active_futures_exchanges:
        send_telegram_message("⚠️ *Bot de Futuros não pôde ser iniciado:* Nenhuma exchange foi conectada com sucesso.")
        return
    send_telegram_message("🚀 *Bot de Arbitragem de Futuros iniciado e rodando em segundo plano!*")
    while True:
        if not futures_running:
            await asyncio.sleep(10)
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
        except Exception as e:
            print(f"[ERRO] Erro no loop de futuros: {e}")
            await asyncio.sleep(60)
        await asyncio.sleep(30)

# ==============================================================================
# 4. LÓGICA DO SERVIDOR WEB (FLASK) E COMANDOS
# ==============================================================================
@app.route(f"/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook_handler():
    data = request.get_json()
    if "message" in data:
        chat_id = data["message"]["chat"]["id"]
        msg_text = data["message"].get("text", "")
        if str(chat_id) == TELEGRAM_CHAT_ID:
            handle_telegram_command(msg_text)
    return "OK", 200

def handle_telegram_command(command_text):
    global futures_running, futures_min_profit_threshold, FUTURES_DRY_RUN
    parts = command_text.strip().lower().split()
    command = parts[0]
    print(f"[INFO] Recebido comando via webhook: {command}")
    if command == "/status_futuros":
        status = "Ativo ✅" if futures_running else "Pausado ⏸️"
        active_exchanges_str = ', '.join([ex.upper() for ex in active_futures_exchanges.keys()])
        msg = (f"📊 *Status do Bot de Futuros*\n\n"
               f"**Status:** `{status}`\n"
               f"**Exchanges Ativas:** `{active_exchanges_str}`\n"
               f"**Pares Monitorados:** `{futures_monitored_pairs_count}`\n"
               f"**Lucro Mínimo:** `{futures_min_profit_threshold:.2f}%`\n"
               f"**Modo:** `{'SIMULAÇÃO' if FUTURES_DRY_RUN else 'REAL'}`")
        send_telegram_message(msg)
    elif command == "/pausar_futuros":
        futures_running = False
        send_telegram_message("⏸️ *Bot de Futuros pausado.*")
    elif command == "/retomar_futuros":
        futures_running = True
        send_telegram_message("▶️ *Bot de Futuros retomado.*")
    # Adicione outros comandos aqui
    else:
        send_telegram_message(f"Comando `{command}` não reconhecido. Use `/status_futuros`, `/pausar_futuros` ou `/retomar_futuros`.")

# ==============================================================================
# 5. INICIALIZAÇÃO
# ==============================================================================
def run_async_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(main_futures_loop())

if __name__ == "__main__":
    # Configura o webhook na inicialização
    set_telegram_webhook()
    
    # Inicia o loop de arbitragem em uma thread separada
    bot_thread = threading.Thread(target=run_async_loop, daemon=True)
    bot_thread.start()
    
    # Inicia o servidor Flask para receber os webhooks
    # O Gunicorn usará este objeto 'app'
    # app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 5000)))
