# -*- coding: utf-8 -*-

import asyncio
import traceback
import nest_asyncio
from decouple import config
from telethon import TelegramClient, events

# Aplica o nest_asyncio para permitir loops de eventos aninhados em ambientes como notebooks.
# É uma boa prática mantê-lo, especialmente para desenvolvimento e testes.
nest_asyncio.apply()

# Tenta importar a biblioteca ccxt. Se não estiver instalada, o programa não pode rodar.
try:
    import ccxt.async_support as ccxt
except ImportError:
    print("[FATAL] A biblioteca 'ccxt' não foi encontrada. Instale-a com: pip install ccxt")
    exit()

# --- 1. CONFIGURAÇÃO ---

# Carrega as credenciais do arquivo .env.
# O bot não funcionará se estas variáveis não estiverem definidas.
try:
    API_ID = int(config('API_ID'))
    API_HASH = config('API_HASH')
    BOT_TOKEN = config('BOT_TOKEN')
    TARGET_CHAT_ID = int(config('TARGET_CHAT_ID'))
except (ValueError, TypeError) as e:
    print(f"[FATAL] Erro ao carregar as configurações do Telegram do arquivo .env. Verifique se API_ID, API_HASH, BOT_TOKEN e TARGET_CHAT_ID estão definidos corretamente. Erro: {e}")
    exit()

# Lista de exchanges que o bot irá monitorar.
# A Huobi foi mantida, mas agora usará a API spot (padrão), que é mais estável.
EXCHANGES_TO_MONITOR = [
    'okx',
    'cryptocom',
    'kucoin',
    'bybit',
    'huobi',
]

# Lista de pares de moedas prioritários para monitoramento.
# O bot tentará encontrar estes pares em comum entre as exchanges ativas.
TARGET_PAIRS = [
    'XRP/USDT','DOGE/USDT','BCH/USDT','LTC/USDT','UNI/USDT',
    'XLM/USDT','BNB/USDT','AVAX/USDT','APT/USDT','AAVE/USDT',
    'ETH/USDT','BTC/USDT','SOL/USDT','ADA/USDT','DOT/USDT',
    'LINK/USDT','MATIC/USDT','ATOM/USDT','FTM/USDT','TRX/USDT',
    'EOS/USDT','NEAR/USDT','ALGO/USDT','VET/USDT','ICP/USDT',
    'FIL/USDT','SAND/USDT','MANA/USDT','THETA/USDT','AXS/USDT'
]

# Valor padrão para simulação de trade em USDT. Pode ser alterado via comando do Telegram.
TRADE_AMOUNT_USDT = 1.0
# Limiar mínimo de lucro percentual para que uma oportunidade seja notificada.
MIN_PROFIT_THRESHOLD = 0.5
# Tempo de espera em segundos entre cada ciclo de verificação.
LOOP_SLEEP_SECONDS = 300  # 5 minutos

# --- 2. INICIALIZAÇÃO ---

# Dicionário global para armazenar as instâncias ativas das exchanges.
active_exchanges = {}

# Inicialização do cliente do Telegram.
telegram_client = TelegramClient('bot_session', API_ID, API_HASH)
telegram_ready = False
telegram_chat_entity = None

# --- 3. LÓGICA DE ARBITRAGEM ---

async def initialize_exchanges():
    """Cria instâncias das classes de exchange da ccxt e as armazena."""
    global active_exchanges
    print("[INFO] Inicializando exchanges...")
    
    for name in EXCHANGES_TO_MONITOR:
        try:
            # A ccxt lida com as variações de nome (ex: okx vs okex)
            exchange_class = getattr(ccxt, name)
            # A configuração padrão ('enableRateLimit': True) é suficiente e usará a API spot.
            # A configuração específica para 'huobi' foi removida para corrigir o erro.
            instance = exchange_class({'enableRateLimit': True})
            active_exchanges[name] = instance
            print(f"[INFO] Instância da exchange '{name}' criada.")
        except AttributeError:
            print(f"[WARN] Exchange '{name}' não encontrada na biblioteca ccxt. Será ignorada.")
        except Exception as e:
            print(f"[ERROR] Falha ao instanciar a exchange '{name}': {e}")

async def load_all_markets():
    """
    Carrega os mercados de todas as exchanges instanciadas.
    Se uma exchange falhar ao carregar, ela é removida da lista de exchanges ativas.
    """
    global active_exchanges
    print("[INFO] Carregando mercados de todas as exchanges...")
    
    tasks = {name: ex.load_markets() for name, ex in active_exchanges.items()}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    
    failed_exchanges = []
    for (name, ex), result in zip(active_exchanges.items(), results):
        if isinstance(result, Exception):
            print(f"[ERROR] Falha ao carregar mercados para '{name}': {result}. A exchange será desativada neste ciclo.")
            failed_exchanges.append(name)
        else:
            print(f"[INFO] Mercados para '{name}' carregados com sucesso ({len(ex.markets)} pares).")

    # Remove as exchanges que falharam e fecha suas conexões
    for name in failed_exchanges:
        if name in active_exchanges:
            await active_exchanges[name].close()
            del active_exchanges[name]

def get_common_pairs():
    """Filtra e retorna os pares de moedas que existem em TODAS as exchanges ativas."""
    if len(active_exchanges) < 2:
        return []
    
    # Cria um conjunto de pares para cada exchange
    sets_of_pairs = [set(ex.markets.keys()) for ex in active_exchanges.values()]
    
    # Encontra a interseção (pares comuns a todas)
    common_symbols = set.intersection(*sets_of_pairs)
    
    # Prioriza os pares da lista TARGET_PAIRS
    monitored_pairs = [p for p in TARGET_PAIRS if p in common_symbols]
    
    print(f"[INFO] Encontrados {len(monitored_pairs)} pares comuns para monitorar: {', '.join(monitored_pairs[:5])}...")
    return monitored_pairs

async def fetch_order_book(exchange_name, symbol):
    """Busca o livro de ofertas para um único par em uma exchange."""
    exchange = active_exchanges.get(exchange_name)
    if not exchange:
        return None
    try:
        # O limite de profundidade pode variar; 5 é um valor seguro e rápido.
        order_book = await exchange.fetch_order_book(symbol, limit=5)
        bid = order_book['bids'][0][0] if order_book.get('bids') else None
        ask = order_book['asks'][0][0] if order_book.get('asks') else None
        
        if bid and ask:
            return {'name': exchange_name, 'symbol': symbol, 'bid': bid, 'ask': ask}
    except Exception as e:
        # Avisos de falha por par são úteis, mas podem poluir o log.
        # print(f"[WARN] Não foi possível buscar o order book para {symbol} em {exchange_name}: {e}")
        pass
    return None

async def fetch_all_order_books(pairs_to_check):
    """Busca todos os livros de ofertas de forma concorrente para os pares fornecidos."""
    tasks = []
    for symbol in pairs_to_check:
        for name in active_exchanges.keys():
            tasks.append(fetch_order_book(name, symbol))
            
    results = await asyncio.gather(*tasks)
    
    # Estrutura os dados para fácil acesso: data['XRP/USDT']['binance'] = {'bid': ..., 'ask': ...}
    structured_data = {}
    for res in results:
        if res:
            structured_data.setdefault(res['symbol'], {})[res['name']] = {'bid': res['bid'], 'ask': res['ask']}
            
    return structured_data

def find_arbitrage_opportunities(data):
    """Analisa os dados coletados e identifica oportunidades de arbitragem."""
    opportunities = []
    for symbol, exchanges_data in data.items():
        if len(exchanges_data) < 2:
            continue

        for buy_exchange, buy_data in exchanges_data.items():
            for sell_exchange, sell_data in exchanges_data.items():
                if buy_exchange == sell_exchange:
                    continue

                buy_price = buy_data.get('ask')
                sell_price = sell_data.get('bid')

                if buy_price and sell_price and buy_price > 0:
                    profit_percent = ((sell_price - buy_price) / buy_price) * 100
                    
                    if profit_percent > MIN_PROFIT_THRESHOLD:
                        opportunities.append({
                            'symbol': symbol,
                            'buy_exchange': buy_exchange.upper(),
                            'buy_price': buy_price,
                            'sell_exchange': sell_exchange.upper(),
                            'sell_price': sell_price,
                            'profit_percent': profit_percent,
                        })
                        
    # Ordena as oportunidades da mais lucrativa para a menos
    return sorted(opportunities, key=lambda x: x['profit_percent'], reverse=True)

# --- 4. TELEGRAM ---

async def send_telegram_message(message):
    """Envia uma mensagem para o chat alvo do Telegram de forma segura."""
    if not telegram_ready or not telegram_chat_entity:
        print("[WARN] Telegram não está pronto. Mensagem não enviada (será impressa no console):")
        print(message)
        return
    try:
        await telegram_client.send_message(telegram_chat_entity, message, parse_mode='md')
    except Exception as e:
        print(f"[ERROR] Falha ao enviar mensagem no Telegram: {e}")

@telegram_client.on(events.NewMessage(pattern='/settrade (\\d+(\\.\\d+)?)'))
async def set_trade_amount_handler(event):
    """Handler para o comando /settrade <valor>."""
    global TRADE_AMOUNT_USDT
    try:
        value = float(event.pattern_match.group(1))
        if 0 < value <= 1000:  # Aumentado o limite para 1000
            TRADE_AMOUNT_USDT = value
            await event.respond(f"✅ Valor de trade para simulação ajustado para **{TRADE_AMOUNT_USDT:.2f} USDT**.")
        else:
            await event.respond("⚠️ Valor inválido. Informe um número maior que 0 e no máximo 1000.")
    except (ValueError, TypeError):
        await event.respond("❌ Erro de formato. Use: `/settrade 10.5`")

@telegram_client.on(events.NewMessage(pattern='/status'))
async def status_handler(event):
    """Handler para o comando /status."""
    active_names = ", ".join(active_exchanges.keys()) if active_exchanges else "Nenhuma"
    msg = (
        f"**🤖 Status do Bot de Arbitragem**\n\n"
        f"**Valor de Trade (Simulação):** `{TRADE_AMOUNT_USDT:.2f} USDT`\n"
        f"**Exchanges Ativas:** `{active_names}`\n"
        f"**Próxima Verificação em:** Aprox. `{(LOOP_SLEEP_SECONDS / 60):.1f}` minutos"
    )
    await event.respond(msg)

# --- 5. LOOP PRINCIPAL E EXECUÇÃO ---

async def main_loop():
    """O loop principal que orquestra a inicialização e a busca contínua por oportunidades."""
    global telegram_ready, telegram_chat_entity
    
    # 1. Tenta se conectar ao Telegram
    try:
        print("[INFO] Conectando ao Telegram...")
        telegram_chat_entity = await telegram_client.get_entity(TARGET_CHAT_ID)
        telegram_ready = True
        print("[INFO] Cliente do Telegram conectado e pronto.")
    except Exception as e:
        print(f"[WARN] Não foi possível conectar ao Telegram: {e}. O bot continuará rodando sem enviar alertas.")

    # 2. Inicializa as exchanges e carrega os mercados
    await initialize_exchanges()
    await load_all_markets()
    
    if len(active_exchanges) < 2:
        msg = "⚠️ **Bot encerrando:** Menos de duas exchanges ativas. Não é possível fazer arbitragem."
        await send_telegram_message(msg)
        return

    common_pairs = get_common_pairs()
    if not common_pairs:
        msg = "⚠️ **Aviso:** Nenhuma moeda em comum foi encontrada entre as exchanges ativas. O bot continuará tentando."
        await send_telegram_message(msg)
    else:
        msg = (
            f"✅ **Bot iniciado com sucesso!**\n\n"
            f"**Exchanges Ativas:** `{', '.join(active_exchanges.keys())}`\n"
            f"**Pares Monitorados:** `{len(common_pairs)}`\n"
            f"Iniciando busca por oportunidades..."
        )
        await send_telegram_message(msg)

    # 3. Loop de monitoramento contínuo
    while True:
        try:
            print(f"\n[{pd.Timestamp.now()}] Iniciando novo ciclo de verificação...")
            order_book_data = await fetch_all_order_books(common_pairs)
            opportunities = find_arbitrage_opportunities(order_book_data)
            
            if opportunities:
                print(f"[SUCCESS] {len(opportunities)} oportunidades encontradas!")
                message = "🤑 **Oportunidades de Arbitragem Detectadas!**\n\n"
                for opp in opportunities[:5]: # Limita a 5 por mensagem para não ser spam
                    profit_usdt = (opp['profit_percent'] / 100) * TRADE_AMOUNT_USDT
                    message += (
                        f"**{opp['symbol']}** | Lucro: **{opp['profit_percent']:.2f}%**\n"
                        f"Compra: `{opp['buy_price']:.6f}` em `{opp['buy_exchange']}`\n"
                        f"Venda: `{opp['sell_price']:.6f}` em `{opp['sell_exchange']}`\n"
                        f"_(Lucro Simulado: ${profit_usdt:.4f} com ${TRADE_AMOUNT_USDT:.2f})_\n---\n"
                    )
                await send_telegram_message(message)
            else:
                print("[INFO] Nenhuma oportunidade lucrativa encontrada neste ciclo.")

        except Exception as e:
            print(f"[ERROR] Ocorreu um erro inesperado no loop principal: {e}")
            traceback.print_exc()
            # Espera um pouco antes de tentar novamente para não sobrecarregar em caso de erro persistente
            await asyncio.sleep(60)

        print(f"Ciclo concluído. Aguardando {LOOP_SLEEP_SECONDS} segundos...")
        await asyncio.sleep(LOOP_SLEEP_SECONDS)

async def shutdown():
    """Fecha todas as conexões abertas de forma limpa."""
    print("\n[INFO] Encerrando o bot...")
    tasks = [ex.close() for ex in active_exchanges.values()]
    await asyncio.gather(*tasks, return_exceptions=True)
    print("[INFO] Conexões com as exchanges foram fechadas.")
    if telegram_client.is_connected():
        await telegram_client.disconnect()
        print("[INFO] Conexão com o Telegram foi fechada.")

async def main():
    """Função principal que gerencia o ciclo de vida do bot."""
    try:
        # Inicia o cliente do Telegram e o loop principal em paralelo
        await telegram_client.start(bot_token=BOT_TOKEN)
        await main_loop()
    except Exception as e:
        print(f"[FATAL] Um erro crítico impediu o bot de iniciar: {e}")
        traceback.print_exc()
    finally:
        await shutdown()

if __name__ == '__main__':
    try:
        # Adiciona pandas para formatação de data/hora no log
        import pandas as pd
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[INFO] Desligamento solicitado pelo usuário (Ctrl+C).")
    except ImportError:
        print("[FATAL] A biblioteca 'pandas' não foi encontrada. Instale-a com: pip install pandas")

