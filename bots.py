# -*- coding: utf-8 -*-
import os
import sys
import time
import hmac
import base64
import requests
import json
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, getcontext, ROUND_DOWN
from dotenv import load_dotenv

# --- Importações Condicionais ---
try:
    import ccxt.async_support as ccxt
except ImportError:
    print("Erro: A biblioteca 'ccxt' não está instalada. O bot de futuros não pode funcionar.")
    print("Instale com: pip install ccxt")
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

API_KEYS_FUTURES = {
    'okx': {'apiKey': os.getenv('OKX_API_KEY'), 'secret': os.getenv('OKX_API_SECRET'), 'password': os.getenv('OKX_API_PASSPHRASE')},
    'gateio': {'apiKey': os.getenv('GATEIO_API_KEY'), 'secret': os.getenv('GATEIO_API_SECRET')},
    'mexc': {'apiKey': os.getenv('MEXC_API_KEY'), 'secret': os.getenv('MEXC_API_SECRET')},
    'bitget': {'apiKey': os.getenv('BITGET_API_KEY'), 'secret': os.getenv('BITGET_API_SECRET'), 'password': os.getenv('BITGET_API_PASSPHRASE')},
}

# --- Variáveis de estado globais ---
futures_running = True
futures_min_profit_threshold = Decimal(os.getenv("FUTURES_MIN_PROFIT_THRESHOLD", "0.3"))
FUTURES_DRY_RUN = os.getenv("FUTURES_DRY_RUN", "true").lower() in ["1", "true", "yes"]
active_futures_exchanges = {}
futures_monitored_pairs_count = 0
last_telegram_update_id = 0

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
        print("[AVISO] Token ou Chat ID do Telegram não configurado. Não é possível enviar mensagem.")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        response = requests.post(url, data={"chat_id": final_chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Erro ao enviar mensagem no Telegram: {e}")

async def get_telegram_updates():
    global last_telegram_update_id
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?offset={last_telegram_update_id + 1}&timeout=20"
    try:
        response = requests.get(url, timeout=25)
        response.raise_for_status()
        data = response.json()
        if data.get("ok") and data.get("result"):
            updates = data["result"]
            if updates:
                last_telegram_update_id = updates[-1]["update_id"]
                return updates
    except requests.exceptions.RequestException as e:
        print(f"Erro ao buscar atualizações do Telegram: {e}")
    return []

# ==============================================================================
# 3. LÓGICA DE ARBITRAGEM DE FUTUROS
# ==============================================================================
async def initialize_futures_exchanges():
    global active_futures_exchanges
    print("[INFO] Inicializando exchanges para o MODO FUTUROS...")
    for name, creds in API_KEYS_FUTURES.items():
        if not creds or not creds.get('apiKey'):
            print(f"[AVISO] Chaves de API para '{name}' não configuradas. Pulando.")
            continue
        
        instance = None
        try:
            exchange_class = getattr(ccxt, name)
            instance = exchange_class({**creds, 'options': {'defaultType': 'swap'}})
            await instance.load_markets()
            active_futures_exchanges[name] = instance
            print(f"[INFO] Exchange '{name}' carregada com sucesso.")
        except Exception as e:
            print(f"[ERRO] Falha ao instanciar '{name}': {e}")
            send_telegram_message(f"❌ *Erro de Conexão (Futuros):* Falha ao conectar em `{name}`: `{e}`")
            if instance:
                await instance.close()

async def find_futures_opportunities():
    if not active_futures_exchanges:
        return []

    tasks = {name: ex.fetch_tickers(FUTURES_TARGET_PAIRS) for name, ex in active_futures_exchanges.items()}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    prices_by_symbol = {}
    for (name, _), res in zip(tasks.items(), results):
        if isinstance(res, Exception):
            print(f"[AVISO] Falha ao buscar tickers da '{name}': {res}")
            continue
        for symbol, ticker in res.items():
            if symbol not in prices_by_symbol:
                prices_by_symbol[symbol] = []
            if ticker.get('bid') and ticker.get('ask'):
                prices_by_symbol[symbol].append({
                    'exchange': name,
                    'bid': Decimal(str(ticker['bid'])),
                    'ask': Decimal(str(ticker['ask']))
                })

    opportunities = []
    for symbol, prices in prices_by_symbol.items():
        if len(prices) < 2:
            continue
        
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

async def main_futures_loop():
    global futures_monitored_pairs_count
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
            print(f"[ERRO] Erro no loop principal de futuros: {e}")
            send_telegram_message(f"⚠️ *Erro no Bot de Futuros:* Ocorreu um erro no loop principal: `{e}`")
            await asyncio.sleep(60) # Espera mais tempo em caso de erro

        await asyncio.sleep(30)

# ==============================================================================
# 4. LÓGICA DE COMANDOS DO TELEGRAM
# ==============================================================================
async def handle_telegram_command(command_text):
    global futures_running, futures_min_profit_threshold, FUTURES_DRY_RUN
    
    parts = command_text.strip().lower().split()
    command = parts[0]
    
    print(f"[INFO] Recebido comando do Telegram: {command}")

    if command == "/ajuda_futuros":
        help_message = (
            "🤖 *Comandos do Bot de Futuros:*\n\n"
            "`/status_futuros` - Vê o status atual do bot.\n"
            "`/setprofit_futuros <valor>` - Ajusta o lucro mínimo (Ex: 0.4).\n"
            "`/pausar_futuros` - Pausa a busca por oportunidades.\n"
            "`/retomar_futuros` - Retoma a busca.\n"
            "`/ping` - Testa a latência do bot.\n"
            "`/testar_conexoes` - Verifica a conexão com as exchanges."
        )
        send_telegram_message(help_message)

    elif command == "/status_futuros":
        status = "Ativo ✅" if futures_running else "Pausado ⏸️"
        active_exchanges_str = ', '.join([ex.upper() for ex in active_futures_exchanges.keys()])
        msg = (
            f"📊 *Status do Bot de Futuros*\n\n"
            f"**Status:** `{status}`\n"
            f"**Exchanges Ativas:** `{active_exchanges_str}`\n"
            f"**Pares Monitorados:** `{futures_monitored_pairs_count}`\n"
            f"**Lucro Mínimo:** `{futures_min_profit_threshold:.2f}%`\n"
            f"**Modo:** `{'SIMULAÇÃO' if FUTURES_DRY_RUN else 'REAL'}`"
        )
        send_telegram_message(msg)

    elif command == "/setprofit_futuros":
        if len(parts) < 2 or not parts[1].replace('.', '', 1).isdigit():
            send_telegram_message("❌ *Uso incorreto:* `/setprofit_futuros <valor>` (Ex: 0.4)")
            return
        new_value = Decimal(parts[1])
        futures_min_profit_threshold = new_value
        send_telegram_message(f"✅ *Bot de Futuros:* Lucro mínimo ajustado para `{new_value:.2f}%`")

    elif command == "/pausar_futuros":
        futures_running = False
        send_telegram_message("⏸️ *Bot de Futuros pausado.*")

    elif command == "/retomar_futuros":
        futures_running = True
        send_telegram_message("▶️ *Bot de Futuros retomado.*")

    elif command == "/ping":
        start_time = time.time()
        send_telegram_message("Pong!")
        end_time = time.time()
        latency = (end_time - start_time) * 1000
        send_telegram_message(f"Latência de resposta: `{latency:.2f} ms`")

    elif command == "/testar_conexoes":
        send_telegram_message("🔍 *Verificando conexões com as exchanges...*")
        results = {}
        for name, creds in API_KEYS_FUTURES.items():
            if not creds or not creds.get('apiKey'):
                continue
            instance = None
            try:
                exchange_class = getattr(ccxt, name)
                instance = exchange_class({**creds, 'options': {'defaultType': 'swap'}})
                await instance.load_markets()
                results[name] = "OK"
            except Exception as e:
                results[name] = f"Erro: {e}"
            finally:
                if instance:
                    await instance.close()
        
        status_msg = "✅ *Status das Conexões (Futuros):*\n\n"
        for ex, status in results.items():
            status_msg += f"`{ex.upper()}`: `{status}`\n"
        send_telegram_message(status_msg)

    else:
        # Ignora comandos não reconhecidos para não interferir com o outro bot
        pass

async def telegram_poll_loop():
    print("[INFO] Iniciando loop de escuta do Telegram...")
    while True:
        try:
            updates = await get_telegram_updates()
            for update in updates:
                if "message" in update and "text" in update["message"]:
                    chat_id = update["message"]["chat"]["id"]
                    if str(chat_id) == TELEGRAM_CHAT_ID:
                        await handle_telegram_command(update["message"]["text"])
        except Exception as e:
            print(f"[ERRO] Erro no loop do Telegram: {e}")
        await asyncio.sleep(1)

# ==============================================================================
# 5. FUNÇÃO PRINCIPAL DE INICIALIZAÇÃO
# ==============================================================================
async def main():
    """Função principal que inicializa e roda todos os componentes."""
    
    # Verifica se as configurações essenciais estão presentes
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[ERRO CRÍTICO] TELEGRAM_TOKEN e TELEGRAM_CHAT_ID devem ser definidos nas variáveis de ambiente.")
        return

    send_telegram_message("🚀 *Bot de Arbitragem de Futuros iniciando...*")
    
    # Inicializa as conexões com as exchanges
    await initialize_futures_exchanges()
    
    if not active_futures_exchanges:
        msg = "⚠️ *Bot de Futuros não pôde ser iniciado:* Nenhuma exchange foi conectada com sucesso."
        print(msg)
        send_telegram_message(msg)
        return

    # Cria as tarefas para rodar em paralelo
    telegram_task = asyncio.create_task(telegram_poll_loop())
    futures_task = asyncio.create_task(main_futures_loop())
    
    # Aguarda as tarefas completarem (o que nunca acontecerá, pois são loops infinitos)
    await asyncio.gather(telegram_task, futures_task)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[INFO] Bot encerrado pelo usuário.")
    finally:
        # Fecha as conexões das exchanges ao encerrar
        async def close_all():
            for ex in active_futures_exchanges.values():
                await ex.close()
        asyncio.run(close_all())
        print("[INFO] Conexões com as exchanges fechadas.")
