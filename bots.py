# v8.1 - O Primeiro Tijolo: Telegram
# Objetivo: Confirmar que a biblioteca python-telegram-bot funciona isoladamente.

import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# 1. Configuração de Log
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

logger.info("--- INÍCIO DO SCRIPT 'O PRIMEIRO TIJOLO' (v8.1) ---")

# 2. Variáveis de Ambiente
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# 3. Comandos Simples
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde ao comando /start."""
    logger.info(f"Comando /start recebido de {update.effective_user.name}")
    await update.message.reply_text('Olá! A biblioteca python-telegram-bot (v8.1) está funcionando.')

async def ajuda_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Responde ao comando /ajuda."""
    logger.info(f"Comando /ajuda recebido de {update.effective_user.name}")
    await update.message.reply_text('Comandos disponíveis: /start, /ajuda')

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
    application.add_handler(CommandHandler("ajuda", ajuda_command))

    logger.info("Iniciando o polling do Telegram... O bot v8.1 está agora ativo.")
    application.run_polling()

if __name__ == "__main__":
    logger.info("Executando o bloco __main__...")
    main()
