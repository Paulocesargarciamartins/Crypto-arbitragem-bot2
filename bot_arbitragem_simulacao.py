import os
import asyncio
from telethon import TelegramClient, events
import ccxt.pro as ccxt
import nest_asyncio

# --- Patch para loops aninhados no Heroku ---
nest_asyncio.apply()

# --- Configurações ---
API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID", "0"))  # substitua pelo ID do seu chat
trade_amount_usdt = 10.0  # valor inicial de trade

# --- Inicializa bot do Telegram ---
client = TelegramClient('bot.session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# --- Inicializa exchanges ---
exchanges = {
    "okx": ccxt.okx(),
    "cryptocom": ccxt.cryptocom(),
}

# --- Função para carregar mercados ---
async def load_markets():
    for name, ex in exchanges.items():
        try:
            await ex.load_markets()
            print(f"[INFO] Mercados carregados: {name} ({len(ex.markets)} mercados)")
        except Exception as e:
            print(f"[ERROR] Falha ao carregar mercados {name}: {e}")

# --- Handler universal do Telegram ---
@client.on(events.NewMessage(incoming=True))
async def universal_handler(event):
    """
    Loga todas as mensagens recebidas.
    Responde a /status e /settrade apenas se vierem do TARGET_CHAT_ID
    ou de conversa privada com o bot.
    """
    global trade_amount_usdt  # <-- correção: deve vir antes de qualquer uso da variável

    try:
        chat_id = getattr(event.chat_id, '__int__', lambda: event.chat_id)()
    except Exception:
        chat_id = event.chat_id
    text = (event.raw_text or "").strip()
    sender = getattr(event.sender_id, None)
    print(f"[MSG] chat_id={chat_id} sender_id={sender} text={text}")

    # Responde só se for do chat configurado (ou se for privado com o bot)
    is_allowed = (chat_id == TARGET_CHAT_ID) or getattr(event, 'is_private', False)
    if not is_allowed:
        return

    # Comando /status
    if text.startswith("/status"):
        msg = f"Valor de trade atual: {trade_amount_usdt} USDT\nExchanges ativas: {', '.join(exchanges.keys())}"
        try:
            await event.respond(msg)
            print("[INFO] Respondeu /status")
        except Exception as e:
            print(f"[ERROR] Falha ao responder /status: {e}")
        return

    # Comando /settrade <valor>
    if text.startswith("/settrade"):
        parts = text.split()
        if len(parts) >= 2:
            try:
                val = float(parts[1])
                if 0 < val <= 100:
                    trade_amount_usdt = val
                    await event.respond(f"Valor de trade ajustado para {trade_amount_usdt} USDT.")
                    print(f"[INFO] trade_amount_usdt ajustado para {trade_amount_usdt}")
                else:
                    await event.respond("Informe um valor entre 0 e 100 USDT.")
            except Exception as e:
                await event.respond(f"Erro ao interpretar valor: {e}")
        else:
            await event.respond("Uso: /settrade 5")
        return

# --- Função principal ---
async def main():
    print("[INFO] Carregando mercados...")
    await load_markets()
    print(f"[INFO] Bot iniciado com {len(exchanges)} exchanges ativas.")
    # Mantém o bot rodando
    await client.run_until_disconnected()

# --- Entry point ---
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("[INFO] Bot interrompido pelo usuário.")
