# bot.py - O Mensageiro do Telegram

import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logging.basicConfig(format='%(asctime)s - BOT - %(levelname)s - %(message)s', level=logging.INFO)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

def write_command(command_text):
    """Escreve um comando para o motor."""
    try:
        with open("command.txt", "w") as f:
            f.write(command_text)
        logging.info(f"Comando '{command_text}' enviado para o motor.")
        return True
    except Exception as e:
        logging.error(f"Erro ao escrever arquivo de comando: {e}")
        return False

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot v9 (Arquitetura de 2 Processos) online.")

async def pausar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if write_command("pausar"):
        await update.message.reply_text("Comando 'pausar' enviado para o motor.")

async def retomar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if write_command("retomar"):
        await update.message.reply_text("Comando 'retomar' enviado para o motor.")

async def modo_real_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if write_command("modo_real"):
        await update.message.reply_text("Comando 'modo_real' enviado para o motor.")

async def setlucro_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        lucro = context.args[0]
        if write_command(f"setlucro {lucro}"):
            await update.message.reply_text(f"Comando 'setlucro {lucro}' enviado para o motor.")
    except (IndexError, ValueError):
        await update.message.reply_text("Uso: /setlucro <valor>")

def main():
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("pausar", pausar_command))
    application.add_handler(CommandHandler("retomar", retomar_command))
    application.add_handler(CommandHandler("modo_real", modo_real_command))
    application.add_handler(CommandHandler("setlucro", setlucro_command))
    
    logging.info("Bot mensageiro (bot.py) iniciado.")
    application.run_polling()

if __name__ == "__main__":
    main()
