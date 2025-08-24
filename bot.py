# bot.py - v10.1 - A Nova Biblioteca (com threading)

import os
import logging
import telebot
import ccxt
import time
from decimal import Decimal, getcontext
import threading

# --- Configuração ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
getcontext().prec = 30

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") # Importante para alertas
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD")

# --- Inicialização das Bibliotecas ---
try:
    bot = telebot.TeleBot(TOKEN)
    # Usamos a versão síncrona do CCXT para evitar conflitos de asyncio
    exchange = ccxt.okx({'apiKey': OKX_API_KEY, 'secret': OKX_API_SECRET, 'password': OKX_API_PASSWORD})
    exchange.load_markets()
    logging.info("Bibliotecas Telebot e CCXT iniciadas com sucesso.")
except Exception as e:
    logging.critical(f"Falha ao iniciar bibliotecas: {e}")
    # Tenta enviar um alerta antes de morrer
    if bot and CHAT_ID:
        try:
            bot.send_message(CHAT_ID, f"ERRO CRÍTICO NA INICIALIZAÇÃO: {e}. O bot não pode iniciar.")
        except Exception as alert_e:
            logging.error(f"Falha ao enviar alerta de erro: {alert_e}")
    exit() # Para o script se a inicialização falhar

# --- Estado do Bot ---
state = {
    'is_running': True,
    'dry_run': True,
    'min_profit': Decimal("0.4")
}

# --- Comandos do Bot ---

@bot.message_handler(commands=['start', 'ajuda'])
def send_welcome(message):
    bot.reply_to(message, "Bot v10.1 (pyTelegramBotAPI) online. Use /status, /pausar, /retomar, /modo_real, /setlucro.")

@bot.message_handler(commands=['status'])
def send_status(message):
    status_text = "Em operação" if state['is_running'] else "Pausado"
    mode_text = "Simulação" if state['dry_run'] else "Modo Real"
    reply = (f"Status: {status_text}\n"
             f"Modo: {mode_text}\n"
             f"Lucro Mínimo: {state['min_profit']}%")
    bot.reply_to(message, reply)

@bot.message_handler(commands=['pausar'])
def pause_bot(message):
    state['is_running'] = False
    bot.reply_to(message, "Motor pausado.")
    logging.info("Motor pausado por comando.")

@bot.message_handler(commands=['retomar'])
def resume_bot(message):
    state['is_running'] = True
    bot.reply_to(message, "Motor retomado.")
    logging.info("Motor retomado por comando.")

@bot.message_handler(commands=['modo_real'])
def set_real_mode(message):
    state['dry_run'] = False
    bot.reply_to(message, "Modo Real ativado.")
    logging.info("Modo Real ativado por comando.")

@bot.message_handler(commands=['setlucro'])
def set_profit(message):
    try:
        profit = message.text.split()[1]
        state['min_profit'] = Decimal(profit)
        bot.reply_to(message, f"Lucro mínimo definido para {state['min_profit']}%")
        logging.info(f"Lucro mínimo alterado para {state['min_profit']}%")
    except:
        bot.reply_to(message, "Uso: /setlucro <valor>")

# --- Loop Principal do Motor ---
def main_loop():
    logging.info("Iniciando loop principal do motor.")
    ciclo_num = 0
    while True:
        try:
            if state['is_running']:
                ciclo_num += 1
                logging.info(f"--- Iniciando Ciclo #{ciclo_num} | Modo: {'Simulação' if state['dry_run'] else 'Real'} | Lucro Mín: {state['min_profit']}% ---")
                
                # A LÓGICA DE TRADE REAL ENTRARIA AQUI
                # Exemplo de verificação de saldo:
                # balance = exchange.fetch_balance()
                # logging.info(f"Saldo USDT: {balance['USDT']['free']}")
                
                time.sleep(30) # Simula um ciclo de 30 segundos
            else:
                logging.info("Motor está pausado...")
                time.sleep(30)
        except Exception as e:
            logging.error(f"Erro no ciclo principal: {e}")
            time.sleep(60)

# --- Iniciar Tudo ---
if __name__ == "__main__":
    logging.info("Iniciando o bot...")
    
    # Cria e inicia a thread para o nosso motor de trade
    engine_thread = threading.Thread(target=main_loop)
    engine_thread.daemon = True  # Permite que o programa principal saia mesmo se a thread estiver rodando
    engine_thread.start()
    
    logging.info("Motor rodando em uma thread. Iniciando polling do Telebot...")
    # O polling do bot bloqueia a thread principal, mantendo o script vivo
    bot.polling(non_stop=True)

