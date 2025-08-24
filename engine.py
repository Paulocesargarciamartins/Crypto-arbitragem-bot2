# engine.py - O Trabalhador da OKX

import os
import asyncio
import logging
import time
from decimal import Decimal, getcontext
import random
import ccxt.async_support as ccxt

# Configuração de Log
logging.basicConfig(format='%(asctime)s - ENGINE - %(levelname)s - %(message)s', level=logging.INFO)

# --- Configurações ---
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD")
TAXA_TAKER = Decimal("0.001")
MOEDA_BASE_OPERACIONAL = 'USDT'
MINIMO_ABSOLUTO_USDT = Decimal("3.1")
MIN_ROUTE_DEPTH = 3
FIAT_CURRENCIES = {'USD', 'EUR', 'BRL'}

# --- Estado do Motor (será lido de arquivos de comando) ---
state = {
    'is_running': True,
    'min_profit': Decimal("0.4"),
    'volume_percent': Decimal("100.0"),
    'max_depth': 3,
    'dry_run': True
}

def read_command_file():
    """Lê comandos do bot do Telegram."""
    global state
    try:
        if os.path.exists("command.txt"):
            with open("command.txt", "r") as f:
                command = f.read().strip()
            logging.info(f"Comando recebido: {command}")
            
            parts = command.split()
            cmd = parts[0]

            if cmd == "pausar":
                state['is_running'] = False
            elif cmd == "retomar":
                state['is_running'] = True
            elif cmd == "modo_real":
                state['dry_run'] = False
            elif cmd == "modo_simulacao":
                state['dry_run'] = True
            elif cmd == "setlucro" and len(parts) > 1:
                state['min_profit'] = Decimal(parts[1])
            
            os.remove("command.txt")
    except Exception as e:
        logging.error(f"Erro ao ler arquivo de comando: {e}")

async def run_engine():
    logging.info("Motor (engine.py) iniciado.")
    exchange = ccxt.okx({'apiKey': OKX_API_KEY, 'secret': OKX_API_SECRET, 'password': OKX_API_PASSWORD})
    
    try:
        await exchange.load_markets()
        logging.info("Conexão com a OKX estabelecida e mercados carregados.")
    except Exception as e:
        logging.critical(f"Falha ao conectar na OKX: {e}. Encerrando o motor.")
        await exchange.close()
        return

    while True:
        read_command_file()
        
        if not state['is_running']:
            logging.info("Motor pausado. Verificando novamente em 30s.")
            await asyncio.sleep(30)
            continue

        logging.info(f"Iniciando ciclo de análise. Lucro min: {state['min_profit']}% | Modo: {'Simulação' if state['dry_run'] else 'Real'}")
        # A lógica completa de arbitragem será adicionada aqui depois.
        await asyncio.sleep(60)
        logging.info("Ciclo de análise concluído.")

    await exchange.close()

if __name__ == "__main__":
    asyncio.run(run_engine())
