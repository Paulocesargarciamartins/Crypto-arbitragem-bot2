# bot.py - v10 - A Nova Biblioteca

import os
import logging
import telebot
import ccxt
import time
from decimal import Decimal, getcontext

# --- Configuração ---
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
getcontext().prec = 30

TOKEN = os.getenv("TELEGRAM_TOKEN")
OKX_API_KEY = os.getenv("OKX_API_KEY")
OKX_API_SECRET = os.getenv("OKX_API_SECRET")
OKX_API_PASSWORD = os.getenv("OKX_API_PASSWORD")

# --- Inicialização das Bibliotecas ---
# Usamos a versão síncrona do CCXT para evitar conflitos
try:
    bot = telebot.TeleBot(TOKEN)
    exchange = ccxt.okx({'apiKey': OKX_API_KEY, 'secret': OKX_API_SECRET, 'password': OKX_API_PASSWORD})
    exchange.load_markets()
    logging.info("Bibliotecas Telebot e CCXT iniciadas com sucesso.")
except Exception as e:
    logging.critical(f"Falha ao iniciar bibliotecas: {e}")
    # Se falhar aqui, o bot não vai rodar.
    # Podemos enviar uma mensagem de alerta se tivermos um CHAT_ID
    # bot.send_message(CHAT_ID, f"Erro crítico na inicialização: {e}")
    exit()


# --- Estado do Bot ---
state = {
    'is_running': True,
    'dry_run': True,
    'min_profit': Decimal("0.4")
}

# --- Comandos do Bot ---

@bot.message_handler(commands=['start', 'ajuda'])
def send_welcome(message):
    bot.reply_to(message, "Bot v10 (pyTelegramBotAPI) online. Use /status, /pausar, /retomar, /modo_real, /setlucro.")

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
    while True:
        try:
            if state['is_running']:
                logging.info(f"Iniciando ciclo de análise... | Modo: {'Simulação' if state['dry_run'] else 'Real'}")
                # AQUI ENTRARIA A LÓGICA DE TRADE
                # Exemplo:
                # balance = exchange.fetch_balance()
                # logging.info(f"Saldo USDT: {balance['USDT']['free']}")
                time.sleep(30) # Simula um ciclo de 30 segundos
            else:
                logging.info("Motor está pausado...")
                time.sleep(30)
        except Exception as e:
            logging.error(f"Erro no ciclo principal: {e}")
            time.sleep(60) # Espera mais tempo se houver erro

# --- Iniciar Tudo ---
if __name__ == "__main__":
    # A biblioteca telebot gerencia o polling em uma thread separada,
    # então podemos rodar nosso loop principal diretamente.
    logging.info("Iniciando polling do Telebot em uma thread separada...")
    bot.polling(non_stop=True, interval=3)
    
    # O código abaixo nunca será alcançado se o polling for non_stop,
    # o que é um comportamento comum. A lógica de trade precisa ser chamada
    # de outra forma, ou o polling precisa ser ajustado.
    # Para resolver isso, vamos usar threading.
    
    import threading
    
    # Cria e inicia a thread para o nosso motor
    engine_thread = threading.Thread(target=main_loop)
    engine_thread.start()
    
    logging.info("Motor rodando em uma thread e bot fazendo polling na thread principal.")
