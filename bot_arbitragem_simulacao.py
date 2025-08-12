import asyncio
from datetime import datetime
from decouple import config
from telethon import TelegramClient
import ccxt.async_support as ccxt

# Configurações do Bot
API_ID = config('API_ID', cast=int)
API_HASH = config('API_HASH')
BOT_TOKEN = config('BOT_TOKEN')
TARGET_CHAT_ID = config('TARGET_CHAT_ID', cast=int)

# Lista de pares reduzida para 30 moedas
pairs_to_track = [
    'XRP/USDT', 'DOGE/USDT', 'BCH/USDT', 'LTC/USDT', 'UNI/USDT',
    'XLM/USDT', 'BNB/USDT', 'AVAX/USDT', 'APT/USDT', 'AAVE/USDT',
    'SOL/USDT', 'SHIB/USDT', 'ATOM/USDT', 'TON/USDT', 'ICP/USDT',
    'ARB/USDT', 'DOT/USDT', 'LINK/USDT', 'ADA/USDT', 'NEAR/USDT',
    'FIL/USDT', 'GRT/USDT', 'XTZ/USDT', 'OP/USDT', 'STX/USDT',
    'SAND/USDT', 'AXS/USDT', 'WLD/USDT', 'PYTH/USDT', 'MANA/USDT'
]

# Exchanges para monitorar
exchanges_to_track = ['lbank', 'gemini', 'okx', 'cryptocom', 'kucoin']

async def send_telegram_message(client, message):
    try:
        await client.send_message(TARGET_CHAT_ID, message)
    except Exception as e:
        print(f"Erro ao enviar mensagem para o Telegram: {e}")

async def get_price(exchange_name, symbol):
    try:
        exchange = getattr(ccxt, exchange_name)()
        order_book = await exchange.fetch_order_book(symbol, limit=1)
        bid = order_book['bids'][0][0] if order_book['bids'] else None
        ask = order_book['asks'][0][0] if order_book['asks'] else None
        await exchange.close()
        return exchange_name, symbol, bid, ask
    except Exception as e:
        print(f"Erro ao obter preço de {symbol} na {exchange_name}: {e}")
        return exchange_name, symbol, None, None

async def main():
    print("Bot de arbitragem iniciado...")
    async with TelegramClient('bot', API_ID, API_HASH) as client:
        await client.start(bot_token=BOT_TOKEN)
        while True:
            try:
                debug_info = []
                tasks = [get_price(exchange_name, symbol) for symbol in pairs_to_track for exchange_name in exchanges_to_track]
                results = await asyncio.gather(*tasks)

                current_symbol = None
                for exchange_name, symbol, bid, ask in results:
                    if symbol != current_symbol:
                        debug_info.append(f"\n{symbol}:")
                        current_symbol = symbol
                    if bid is not None and ask is not None:
                        debug_info.append(f" - {exchange_name}: Compra: {bid:.8f} | Venda: {ask:.8f}")

                current_time = datetime.now().strftime("%d-%m-%Y %H:%M:%S")
                report_title = f"CryptoAlerts bot 2: 🔎 Informações de Debug\nData e Hora: {current_time}\n"
                message = report_title + "\n".join(debug_info)

                await send_telegram_message(client, message)

                print("Ciclo completo. Aguardando 1 minuto...")
            except Exception as e:
                print(f"Erro no loop principal: {e}")
            await asyncio.sleep(60)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot finalizado manualmente.")
    except Exception as e:
        print(f"Erro fatal no bot: {e}")
