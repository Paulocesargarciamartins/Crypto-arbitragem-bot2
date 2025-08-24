# v8.2 - O Confronto: Telegram + CCXT
# Objetivo: Testar a interação das duas bibliotecas.

import os
import logging
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
import ccxt.async_support as ccxt

# 1. Configuração de Log
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

logger.info("--- INÍCIO DO SCRIPT 'O CONFRONTO' (v8.2) ---")

# 2. Variáveis de Ambiente
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD")

# 3. Comandos
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde ao comando /start."""
    logger.info(f"Comando /start recebido de {update.effective_user.name}")
    await update.message.reply_text('Bot de teste v8.2 (Telegram + CCXT) online. Use /testar_okx')

async def testar_okx_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Tenta conectar na OKX quando o comando é recebido."""
    logger.info("Comando /testar_okx recebido. Iniciando teste de conexão...")
    await update.message.reply_text("Iniciando teste de conexão com a OKX...")

    exchange = None
    try:
        logger.info("Criando instância do ccxt.okx...")
        await update.message.reply_text("1/3 - Criando instância da exchange...")
        exchange = ccxt.okx({
            'apiKey': OKX_API_KEY, 'secret': OKX_API_SECRET, 'password': OKX_API_PASSWORD
        })

        logger.info("Tentando chamar exchange.fetch_balance()...")
        await update.message.reply_text("2/3 - Chamando a API da OKX (fetch_balance)...")
        
        # Esta é a chamada que provavelmente irá travar ou falhar
        balance = await exchange.fetch_balance()
        
        logger.info("SUCESSO! A conexão com a OKX funcionou.")
        await update.message.reply_text(f"3/3 - SUCESSO! Conexão com a OKX funcionou. Saldo total em USDT: {balance.get('USDT', {}).get('total')}")

    except Exception as e:
        logger.critical(f"FALHA na conexão com a OKX: {e}", exc_info=True)
        await update.message.reply_text(f"FALHA! A conexão com a OKX falhou. Erro: {e}")
    
    finally:
        if exchange:
            logger.info("Fechando a sessão da exchange.")
            await exchange.close()

# 4. Função Principal (main)
def main() -> None:
    """Ponto de entrada do bot."""
    if not TELEGRAM_TOKEN:
        logger.critical("ERRO CRÍTICO: TELEGRAM_TOKEN não encontrado.")
        return

    logger.info("Criando a aplicação do Telegram...")
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    logger.info("Adicionando handlers de comando...")
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("testar_okx", testar_okx_command))

    logger.info("Iniciando o polling do Telegram... O bot v8.2 está agora ativo.")
    application.run_polling()

if __name__ == "__main__":
    logger.info("Executando o bloco __main__...")
    main()
