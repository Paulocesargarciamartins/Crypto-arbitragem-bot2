# v8.0 - O Pulso
# Objetivo: Testar a execução mais básica possível de um script Python no worker do Heroku.

import logging
import time
import os

# Configura o log para aparecer no Heroku
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# A mensagem de teste mais importante
# Se esta mensagem não aparecer nos logs, NADA neste arquivo foi executado.
logging.info("--- INÍCIO DO SCRIPT 'O PULSO' (v8.0) ---")

# Verificação de uma variável de ambiente para garantir que estão sendo lidas
telegram_token_presente = "Sim" if os.getenv("TELEGRAM_TOKEN") else "Não"
logging.info(f"Variável TELEGRAM_TOKEN encontrada? {telegram_token_presente}")

logging.info("Entrando em loop infinito para manter o dyno ativo.")
logging.info("Se você vê esta mensagem, o script está rodando.")

count = 0
while True:
    count += 1
    logging.info(f"Pulso... {count}")
    # Pausa por 30 segundos para não poluir os logs
    time.sleep(30)

