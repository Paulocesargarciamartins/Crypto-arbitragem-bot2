import asyncio
import logging

# Configuração de logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# --- Funções do seu projeto (manter as originais) ---
# Essas são funções simuladas para o exemplo.
# Você deve substituí-las pelas suas funções reais de chamada de API.
async def fetch_ticker_from_okx(pair):
    """Simula a chamada de API da OKX."""
    if pair == 'BTC/USDT':
        await asyncio.sleep(2)
        return {'error': 'API rate limit exceeded'}
    else:
        await asyncio.sleep(1)
        return {'bid': 60000.0, 'ask': 60001.0}

async def fetch_ticker_from_kraken(pair):
    """Simula a chamada de API da Kraken."""
    await asyncio.sleep(1)
    return {'bid': 59990.0, 'ask': 59991.0}

# --- A função de checagem corrigida ---
async def check_rest_for_crypto(exchange, pair):
    """
    Função de checagem de dados para um par de criptomoedas,
    com tratamento robusto de erros de API.
    """
    try:
        if exchange == 'okx':
            api_function = fetch_ticker_from_okx
        elif exchange == 'kraken':
            api_function = fetch_ticker_from_kraken
        else:
            logging.warning(f"Exchange {exchange} não suportada. Pulando.")
            return

        response = await asyncio.wait_for(api_function(pair), timeout=10)

        # Trata o caso em que a API retorna um dicionário de erro
        if isinstance(response, dict) and 'error' in response:
            logging.warning(f"Falha na checagem REST para {pair} em {exchange}: {response['error']}. Pulando.")
            return

        bid = response.get('bid')
        ask = response.get('ask')
        if bid and ask:
            logging.info(f"✅ Dados atualizados para {pair} em {exchange}: BID={bid} ASK={ask}")
            # Adicione aqui sua lógica de cálculo de arbitragem, etc.
        else:
            logging.warning(f"Resposta inesperada da API para {pair} em {exchange}. Pulando.")

    except asyncio.TimeoutError:
        logging.warning(f"Requisição para {pair} em {exchange} demorou muito (timeout). Pulando.")
    except Exception as e:
        logging.error(f"Erro inesperado na checagem para {pair} em {exchange}: {e}. Pulando.")


# --- Código principal ---
async def main():
    """
    Orquestra a execução de todas as checagens de forma assíncrona.
    """
    pairs_to_check = [
        ('okx', 'BTC/USDT'),
        ('okx', 'ETH/USDT'),
        ('kraken', 'BTC/USDT'),
        ('okx', 'XRP/USDT'),
        ('kraken', 'ETH/USDT'),
    ]

    tasks = [check_rest_for_crypto(exchange, pair) for exchange, pair in pairs_to_check]
    
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())

