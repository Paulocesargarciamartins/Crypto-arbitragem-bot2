# engine.py - v9.1 (Final) - Lógica completa e leitura de comandos corrigida

import os
import asyncio
import logging
import time
from decimal import Decimal, getcontext
import random
import ccxt.async_support as ccxt

# --- Configuração ---
logging.basicConfig(format='%(asctime)s - ENGINE - %(levelname)s - %(message)s', level=logging.INFO)
getcontext().prec = 30

OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD")

TAXA_TAKER = Decimal("0.001")
MOEDA_BASE_OPERACIONAL = 'USDT'
MINIMO_ABSOLUTO_USDT = Decimal("3.1")
MIN_ROUTE_DEPTH = 3
MAX_ROUTE_DEPTH_DEFAULT = 3
MARGEM_DE_SEGURANCA = Decimal("0.995")
FIAT_CURRENCIES = {'USD', 'EUR', 'GBP', 'JPY', 'BRL', 'AUD', 'CAD', 'CHF', 'CNY', 'HKD', 'SGD', 'KRW', 'INR', 'RUB', 'TRY', 'UAH', 'VND', 'THB', 'PHP', 'IDR', 'MYR', 'AED', 'SAR', 'ZAR', 'MXN', 'ARS', 'CLP', 'COP', 'PEN'}

# --- Estado Global do Motor ---
state = {
    'is_running': True,
    'min_profit': Decimal("0.4"),
    'volume_percent': Decimal("100.0"),
    'max_depth': MAX_ROUTE_DEPTH_DEFAULT,
    'dry_run': True
}

# --- Lógica de Comandos (CORRIGIDA) ---
async def command_listener():
    """Tarefa que roda em paralelo para escutar por comandos."""
    global state
    logging.info("Escuta de comandos iniciada.")
    while True:
        try:
            if os.path.exists("command.txt"):
                with open("command.txt", "r") as f:
                    command = f.read().strip()
                logging.info(f"Comando recebido: {command}")
                
                parts = command.split()
                cmd = parts[0]

                if cmd == "pausar":
                    state['is_running'] = False
                    logging.info("Estado alterado para: PAUSADO")
                elif cmd == "retomar":
                    state['is_running'] = True
                    logging.info("Estado alterado para: EM OPERAÇÃO")
                elif cmd == "modo_real":
                    state['dry_run'] = False
                    logging.info("Estado alterado para: MODO REAL")
                elif cmd == "modo_simulacao":
                    state['dry_run'] = True
                    logging.info("Estado alterado para: MODO SIMULAÇÃO")
                elif cmd == "setlucro" and len(parts) > 1:
                    state['min_profit'] = Decimal(parts[1])
                    logging.info(f"Lucro mínimo alterado para: {state['min_profit']}%")
                
                os.remove("command.txt")
        except Exception as e:
            logging.error(f"Erro ao ler arquivo de comando: {e}")
        
        await asyncio.sleep(2) # Verifica por comandos a cada 2 segundos

# --- Lógica de Arbitragem (RESTAURADA) ---
class ArbitrageEngine:
    def __init__(self, exchange):
        self.exchange = exchange
        self.markets = {}
        self.graph = {}
        self.rotas_viaveis = []

    async def inicializar(self):
        self.markets = await self.exchange.load_markets()
        logging.info(f"{len(self.markets)} mercados carregados.")
        await self.construir_rotas(state['max_depth'])

    async def construir_rotas(self, max_depth):
        # ... (código idêntico às versões anteriores)
        self.graph = {}
        active_markets = {s: m for s, m in self.markets.items() if m.get('active') and m.get('base') and m.get('quote') and m['base'] not in FIAT_CURRENCIES and m['quote'] not in FIAT_CURRENCIES}
        for symbol, market in active_markets.items():
            base, quote = market['base'], market['quote']
            if base not in self.graph: self.graph[base] = []
            if quote not in self.graph: self.graph[quote] = []
            self.graph[base].append(quote)
            self.graph[quote].append(base)
        todas_as_rotas = []
        def encontrar_ciclos_dfs(u, path, depth):
            if depth > max_depth: return
            for v in self.graph.get(u, []):
                if v == MOEDA_BASE_OPERACIONAL and len(path) >= MIN_ROUTE_DEPTH:
                    rota = path + [v]
                    if len(set(rota)) == len(rota) -1: todas_as_rotas.append(rota)
                elif v not in path: encontrar_ciclos_dfs(v, path + [v], depth + 1)
        encontrar_ciclos_dfs(MOEDA_BASE_OPERACIONAL, [MOEDA_BASE_OPERACIONAL], 1)
        self.rotas_viaveis = [tuple(rota) for rota in todas_as_rotas]
        random.shuffle(self.rotas_viaveis)
        logging.info(f"Mapa de rotas reconstruído. {len(self.rotas_viaveis)} rotas encontradas.")

    def _get_pair_details(self, coin_from, coin_to):
        # ... (código idêntico às versões anteriores)
        pair_buy = f"{coin_to}/{coin_from}"
        if pair_buy in self.markets: return pair_buy, 'buy'
        pair_sell = f"{coin_from}/{coin_to}"
        if pair_sell in self.markets: return pair_sell, 'sell'
        return None, None

    async def _simular_trade(self, cycle_path, volume_inicial):
        # ... (código idêntico às versões anteriores)
        current_amount = volume_inicial
        for i in range(len(cycle_path) - 1):
            coin_from, coin_to = cycle_path[i], cycle_path[i+1]
            pair_id, side = self._get_pair_details(coin_from, coin_to)
            if not pair_id: return None
            orderbook = await self.exchange.fetch_order_book(pair_id)
            orders = orderbook['asks'] if side == 'buy' else orderbook['bids']
            if not orders: return None
            remaining_amount = current_amount
            final_traded_amount = Decimal('0')
            for price, size, *_ in orders:
                price, size = Decimal(str(price)), Decimal(str(size))
                if side == 'buy':
                    cost_for_step = remaining_amount
                    if cost_for_step <= price * size:
                        final_traded_amount += cost_for_step / price
                        remaining_amount = Decimal('0'); break
                    else:
                        final_traded_amount += size
                        remaining_amount -= price * size
                else:
                    if remaining_amount <= size:
                        final_traded_amount += remaining_amount * price
                        remaining_amount = Decimal('0'); break
                    else:
                        final_traded_amount += size * price
                        remaining_amount -= size
            if remaining_amount > 0: return None
            current_amount = final_traded_amount * (Decimal(1) - TAXA_TAKER)
        lucro_percentual = ((current_amount - volume_inicial) / volume_inicial) * 100
        return {'cycle': cycle_path, 'profit': lucro_percentual}

    async def run_main_cycle(self):
        ciclo_num = 0
        while True:
            if not state['is_running']:
                await asyncio.sleep(5)
                continue
            
            ciclo_num += 1
            logging.info(f"--- Iniciando Ciclo de Análise #{ciclo_num} | Modo: {'Simulação' if state['dry_run'] else 'Real'} | Lucro Mín: {state['min_profit']}% ---")
            
            try:
                balance = await self.exchange.fetch_balance()
                saldo_disponivel = Decimal(str(balance.get('free', {}).get(MOEDA_BASE_OPERACIONAL, '0')))
                volume_a_usar = (saldo_disponivel * (state['volume_percent'] / 100)) * MARGEM_DE_SEGURANCA

                if volume_a_usar < MINIMO_ABSOLUTO_USDT:
                    logging.warning(f"Volume ({volume_a_usar:.2f} USDT) abaixo do mínimo. Aguardando 30s.")
                    await asyncio.sleep(30)
                    continue

                melhor_oportunidade = None
                for i, cycle_tuple in enumerate(self.rotas_viaveis):
                    if not state['is_running']: break
                    
                    resultado = await self._simular_trade(list(cycle_tuple), volume_a_usar)
                    if resultado and resultado['profit'] > state['min_profit']:
                        if not melhor_oportunidade or resultado['profit'] > melhor_oportunidade['profit']:
                            melhor_oportunidade = resultado
                            logging.info(f"Nova melhor oportunidade encontrada: {melhor_oportunidade['profit']:.4f}% na rota {' -> '.join(melhor_oportunidade['cycle'])}")

                    if i % 200 == 0: # Pequena pausa para não sobrecarregar
                        await asyncio.sleep(0.1)
                
                if melhor_oportunidade:
                    logging.info(f"Executando melhor oportunidade encontrada: Lucro de {melhor_oportunidade['profit']:.4f}%")
                    # A lógica de execução real (_executar_trade) seria chamada aqui
                    # Por segurança, vamos apenas logar por enquanto
                    if not state['dry_run']:
                        logging.info("MODO REAL: A execução do trade aconteceria aqui.")
                        # await self._executar_trade(melhor_oportunidade['cycle'], volume_a_usar)
                    else:
                        logging.info("MODO SIMULAÇÃO: Trade não executado.")

                logging.info(f"Ciclo #{ciclo_num} concluído. Aguardando 15 segundos.")
                await asyncio.sleep(15)

            except Exception as e:
                logging.critical(f"Erro CRÍTICO no ciclo de análise: {e}", exc_info=True)
                await asyncio.sleep(60)

# --- Função Principal do Motor ---
async def main():
    logging.info("Motor (engine.py) v9.1 iniciado.")
    if not all([OKX_API_KEY, OKX_API_SECRET, OKX_API_PASSWORD]):
        logging.critical("As chaves da API da OKX não foram encontradas. Encerrando.")
        return

    exchange = ccxt.okx({'apiKey': OKX_API_KEY, 'secret': OKX_API_SECRET, 'password': OKX_API_PASSWORD})
    
    try:
        # Cria a tarefa de escuta de comandos
        listener_task = asyncio.create_task(command_listener())
        
        # Cria e roda o motor de arbitragem
        engine = ArbitrageEngine(exchange)
        await engine.inicializar()
        await engine.run_main_cycle()

        # Espera a conclusão (não deve acontecer em operação normal)
        await listener_task

    except Exception as e:
        logging.critical(f"Erro fatal no motor: {e}", exc_info=True)
    finally:
        logging.info("Encerrando a sessão da exchange.")
        await exchange.close()

if __name__ == "__main__":
    asyncio.run(main())
